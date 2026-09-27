"""Twilio Voice webhooks.

    GET|POST /api/telephony/twilio/outbound   call answered -> Fish greeting + <Gather input="speech">
    POST     /api/telephony/twilio/gather     caller speech -> grounded engine -> Fish reply
    POST     /api/telephony/twilio/status     status callbacks (idempotent, monotonic, records duration)
    GET      /api/telephony/twilio/health     safe configuration summary (no secrets)

Every webhook requires a valid ``X-Twilio-Signature`` computed over the exact public URL
(``PUBLIC_BASE_URL`` + path + query) and the form parameters. Caller audio is recognised by
Twilio's ``<Gather>``; the application answers only with verified Fish Speech assets.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..core.config import normalize_e164
from ..services.call_state import TERMINAL, CallStatus
from .twilio import verify_twilio_signature

router = APIRouter(prefix='/api/telephony/twilio', tags=['twilio'])
HANGUP = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def _runtime(request: Request):
    return request.app.state.runtime


def _xml(body: str) -> Response:
    return Response(body, media_type='application/xml')


async def _form(request: Request) -> dict[str, str]:
    runtime = _runtime(request)
    settings = runtime.settings
    if not settings.twilio_enabled:
        raise HTTPException(503, 'telephony_disabled')
    data = {str(key): str(value) for key, value in (await request.form()).items()}
    if not settings.twilio_validate_signature:
        raise HTTPException(503, 'twilio_signature_validation_disabled')   # never accept unsigned webhooks
    url = settings.resolve().public_origin + request.url.path
    if request.url.query:
        url += '?' + request.url.query
    if not verify_twilio_signature(url, data, request.headers.get('X-Twilio-Signature'), settings.twilio_auth_token):
        raise HTTPException(403, 'invalid_twilio_signature')
    return data


@router.get('/health')
async def health(request: Request):
    return _runtime(request).telephony.health()


@router.api_route('/outbound', methods=['GET', 'POST'])
async def outbound(request: Request):
    rt = _runtime(request)
    data = await _form(request)
    session_id = request.query_params.get('session_id')
    call_sid = data.get('CallSid') or None
    call = await asyncio.to_thread(rt.repo.get_call, session_id) if session_id else None
    if call is None and call_sid:
        call = await asyncio.to_thread(rt.repo.get_call_by_provider_id, call_sid)
    if call is None:
        if session_id or not call_sid:
            return _xml(HANGUP)
        # Inbound call to the Twilio number: create the session here.
        caller = normalize_e164(data.get('From', ''), rt.settings.default_country_code) or data.get('From') or 'unknown'
        reserved = await asyncio.to_thread(
            rt.repo.create_inbound_call, provider_call_id=call_sid, provider_conversation_id=None,
            customer_number=caller, from_number=normalize_e164(data.get('To', ''), rt.settings.default_country_code),
            policy=rt.telephony.policy(rate_limit=False))
        call = reserved.call
        if call is None:
            return _xml(HANGUP)
        if reserved.blocked:
            return _xml(rt.telephony.twiml_turn(call.id, ['lines_busy'], hangup=True))
    elif call_sid and call.provider_call_id is None:
        await asyncio.to_thread(rt.repo.attach_provider_call, call.id, provider_call_id=call_sid,
                                provider_conversation_id=None)
    if CallStatus(call.status) in TERMINAL:
        return _xml(HANGUP)
    await asyncio.to_thread(rt.repo.transition, call.id, CallStatus.ANSWERED)
    state = await asyncio.to_thread(rt.repo.load_session, call.id) or rt.engine.new_state(call.direction)
    reply = rt.engine.greeting(state)
    await asyncio.to_thread(rt.repo.save_session, call.id, state)
    await asyncio.to_thread(rt.repo.add_event, call.id, 'twilio_answer_webhook', {'prompts': reply.prompt_ids})
    await asyncio.to_thread(rt.repo.add_turn, call.id, {'turn_index': 0, 'intent': 'greeting', 'prompts': reply.prompt_ids})
    return _xml(rt.telephony.twiml_turn(call.id, reply.prompt_ids, hangup=False))


@router.post('/gather')
async def gather(request: Request):
    rt = _runtime(request)
    data = await _form(request)
    session_id = request.query_params.get('session_id') or ''
    call = await asyncio.to_thread(rt.repo.get_call, session_id) if session_id else None
    if call is None or CallStatus(call.status) in TERMINAL:
        return _xml(HANGUP)
    transcript = (data.get('SpeechResult') or '').strip()
    state = await asyncio.to_thread(rt.repo.load_session, call.id) or rt.engine.new_state(call.direction)
    reply = rt.engine.respond(state, transcript)
    caller = call.customer_number
    if reply.opt_out:
        await asyncio.to_thread(rt.repo.record_opt_out, caller, source='voice', call_id=call.id)
    if reply.appointment:
        await asyncio.to_thread(rt.repo.add_appointment_request, call_id=call.id, caller=caller, details=reply.appointment)
    if reply.callback_topic:
        await asyncio.to_thread(rt.repo.add_callback, source='phone', contact=caller, topic=reply.callback_topic,
                                preferred_window='To be arranged', call_id=call.id)
    await asyncio.to_thread(rt.repo.save_session, call.id, reply.state)
    await asyncio.to_thread(rt.repo.add_turn, call.id, {
        'turn_index': int(reply.state.get('turns') or 0), 'transcript': transcript or None, 'intent': reply.intent,
        'prompts': reply.prompt_ids})
    if reply.hangup:
        await asyncio.to_thread(rt.repo.transition, call.id, CallStatus.ENDING, reason=reply.intent)
    return _xml(rt.telephony.twiml_turn(call.id, reply.prompt_ids, hangup=reply.hangup))


@router.post('/status')
async def status(request: Request):
    rt = _runtime(request)
    data = await _form(request)
    await asyncio.to_thread(rt.telephony.apply_status, request.query_params.get('session_id'), data)
    return Response(status_code=200)
