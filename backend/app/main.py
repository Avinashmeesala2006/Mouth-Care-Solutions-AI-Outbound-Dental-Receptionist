"""Mouth Care Solutions AI Receptionist - FastAPI application.

Telephony: Twilio Voice API (signed TwiML and status webhooks).
Speech: Twilio <Gather> speech recognition -> grounded receptionist engine -> Fish Speech voice pack.
State: PostgreSQL (in-memory only in development without DATABASE_URL).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .api.readiness import live_call_readiness
from .api.security import admin_status, check_login, require_admin, session_token
from .core.config import mask_phone, normalize_e164, settings
from .demo_page import DEMO_HTML
from .runtime import Runtime
from .schemas.api import (
    AdminLoginRequest,
    AgentRequest,
    CallbackRequest,
    CallRequest,
    CancelRequest,
    ConfirmRequest,
    ConsentRequest,
    DncRequest,
    HoldRequest,
    KnowledgeRequest,
    OptOutClearRequest,
    RescheduleRequest,
)
from .services.engine import CLINIC, SERVICES, Agent
from .services.repository import Repo
from .services.usage import threshold_label
from .telephony.twilio import TwilioError
from .telephony.twilio_routes import router as twilio_router

logger = logging.getLogger(__name__)
_app_logger = logging.getLogger('backend.app')
if not _app_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    _app_logger.addHandler(_handler)
    _app_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

INSTANCE_ID = secrets.token_hex(8)
_ASSET_ID = re.compile(r'^[a-z][a-z_]{1,63}$')
repo = Repo()            # in-memory demo booking engine for the web client (development only)
agent = Agent(repo)
runtime = Runtime(settings)
runtime.instance_id = INSTANCE_ID


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = runtime
    runtime.instance_id = INSTANCE_ID
    await runtime.startup()
    yield
    await runtime.shutdown()


app = FastAPI(title='Mouth Care Solutions AI Receptionist', version='4.0.0', lifespan=lifespan)
app.state.runtime = runtime
app.include_router(twilio_router)


def err(e):
    raise HTTPException(400, str(e))


# Public pages and health ----------------------------------------------------------------
@app.get('/', response_class=HTMLResponse)
def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0; url=/demo">'
                        '<a href="/demo">Open Mouth Care Solutions receptionist demo</a>')


@app.get('/favicon.ico')
def favicon():
    return HTMLResponse('', status_code=204)


@app.get('/demo', response_class=HTMLResponse)
def demo():
    return HTMLResponse(DEMO_HTML)


@app.get('/health')
def health():
    return {'status': 'ok', 'mode': settings.mode, 'telephony': 'twilio' if settings.twilio_enabled else 'disabled',
            'instance_id': INSTANCE_ID}


@app.get('/ready')
async def ready():
    cfg = settings.resolve()
    pack = runtime.voice_pack()
    db_ok = await asyncio.to_thread(runtime.repo.healthy)
    migrations = await asyncio.to_thread(runtime.repo.migration_status) if db_ok else {}
    fish = await runtime.fish_health()
    migrations_ok = db_ok and not migrations.get('pending') and not migrations.get('modified')
    voice_ok = pack.valid and (fish.ready or not settings.fish_speech_live_synthesis)
    ready_now = not cfg.errors and db_ok and migrations_ok and (voice_ok if settings.twilio_enabled else True)
    body = {
        'status': 'ready' if ready_now else 'not_ready',
        'scope': 'application (live-call readiness: GET /api/twilio/preflight)',
        'mode': cfg.app_mode,
        'config_errors': cfg.errors,
        'config_warnings': cfg.warnings,
        'database': {'backend': runtime.repo.backend, 'healthy': db_ok, 'migrations': migrations},
        'booking_repository': 'in-memory-demo' if settings.is_development else 'unavailable-live',
        'booking_repository_ready': settings.is_development,
        'providers': {'telephony': cfg.call_provider, 'stt': 'twilio-gather', 'ai': runtime.engine.name,
                      'tts': 'fish-speech', 'notifications': settings.notification_provider},
        'fish_speech': fish.as_dict() | {'required_during_calls': settings.fish_speech_live_synthesis},
        'voice_pack': pack.summary(),
    }
    return JSONResponse(body, status_code=200 if ready_now else 503)


@app.get('/api/voice/fish/health')
async def fish_health():
    return (await runtime.fish_health(max_age=0)).as_dict()


def _packaged_audio(asset_id: str, include_body: bool) -> Response:
    pack = runtime.voice_pack()
    asset = pack.assets.get(asset_id) if _ASSET_ID.match(asset_id) else None
    if not asset:
        raise HTTPException(404, 'audio_not_found')
    return Response(content=asset.content if include_body else b'', media_type='audio/wav',
                    headers={'Cache-Control': 'no-store', 'X-Voice-Asset-SHA256': asset.sha256,
                             'Content-Length': str(len(asset.content))})


@app.get('/api/telephony/audio/{asset_id}')
def packaged_audio(asset_id: str):
    """Verified Fish Speech voice assets served to Twilio as public WAV files."""
    return _packaged_audio(asset_id, include_body=True)


@app.head('/api/telephony/audio/{asset_id}', include_in_schema=False)
def packaged_audio_head(asset_id: str):
    return _packaged_audio(asset_id, include_body=False)


# Web receptionist and demo booking ----------------------------------------------------------
def _agent_response(message: str, session_id: str) -> dict:
    result = agent.respond(message, session_id)
    if not settings.is_development and result.get('intent') == 'appointment':
        return {'text': 'I can record an appointment request for the clinic team to confirm, but live appointment '
                        'availability is not connected.', 'intent': 'appointment_request', 'session_id': session_id}
    return result


def _require_booking_repository() -> None:
    if not settings.is_development:
        raise HTTPException(503, detail={'error': 'live_booking_repository_unavailable',
                                         'message': 'Live appointment availability is not connected.'})


@app.post('/api/agent/session')
def agent_session(r: AgentRequest):
    return _agent_response(r.message, r.session_id or 'demo-session')


@app.post('/api/knowledge/search')
def search(r: KnowledgeRequest):
    return _agent_response(r.query, 'knowledge-search')


@app.get('/api/slots')
def slots():
    _require_booking_repository()
    return {'slots': [repo.slot_view(s) for s in repo.available()]}


@app.post('/api/booking/hold')
def hold(r: HoldRequest):
    _require_booking_repository()
    try:
        return repo.hold(r.slot_id, r.session_id, r.expiry_seconds)
    except ValueError as e:
        err(e)


@app.post('/api/booking/confirm')
def confirm(r: ConfirmRequest):
    _require_booking_repository()
    try:
        return repo.confirm(r.hold_token, r.patient_details, r.consent, r.idempotency_key)
    except ValueError as e:
        err(e)


@app.post('/api/booking/reschedule')
def reschedule(r: RescheduleRequest):
    _require_booking_repository()
    try:
        return repo.reschedule(r.booking_reference, r.new_slot_id, r.verification)
    except ValueError as e:
        err(e)


@app.post('/api/booking/cancel')
def cancel(r: CancelRequest):
    _require_booking_repository()
    try:
        return repo.cancel(r.booking_reference, r.reason, r.verification)
    except ValueError as e:
        err(e)


@app.post('/api/callback')
async def callback(r: CallbackRequest):
    item = await asyncio.to_thread(runtime.repo.add_callback, source='web', contact=r.patient_contact, topic=r.topic,
                                   preferred_window=r.preferred_window)
    repo.log('callback_created', {'id': item['id']})
    return {'id': item['id'], 'patient_contact': r.patient_contact, 'topic': r.topic, 'preferred_window': r.preferred_window,
            'status': item['status']}


# Live-call readiness ----------------------------------------------------------------------------
@app.get('/api/twilio/preflight')
async def telephony_preflight(request: Request):
    destination = request.query_params.get('destination')
    if destination is not None and not normalize_e164(destination, settings.default_country_code):
        raise HTTPException(422, 'destination_must_be_a_valid_phone_number')
    verify_remote = request.query_params.get('verify_remote') == '1'
    return await live_call_readiness(runtime, destination, verify_remote=verify_remote)


@app.get('/api/status')
async def public_status():
    """Cheap status for the web client (no destination and no provider call)."""
    readiness = await live_call_readiness(runtime)
    keys = ('SOFTWARE_READY_FOR_LIVE_CALL', 'LIVE_CALL_ALLOWED', 'SOFTWARE_BLOCKERS', 'BLOCKERS', 'TWILIO_ENABLED',
            'TWILIO_CONFIGURED', 'DATABASE_READY', 'CHECKED_AT')
    summary = {k: readiness.get(k) for k in keys}
    summary['BLOCKERS'] = summary['SOFTWARE_BLOCKERS']   # the public page never sees destination details
    summary['LIVE_CALL_ALLOWED'] = summary['SOFTWARE_READY_FOR_LIVE_CALL']
    admin_enabled, _ = admin_status()
    return {'mode': 'live' if settings.twilio_enabled else 'demo',
            'app_mode': settings.mode,
            'telephony': 'twilio' if settings.twilio_enabled else 'disabled',
            'voice': 'fish-speech-voice-pack' if readiness['VOICE_PACK_VALID'] else 'voice_pack_invalid',
            'voice_pack_valid': readiness['VOICE_PACK_VALID'],
            'fish_speech_ready': readiness['FISH_SPEECH_READY'],
            'booking_repository_ready': settings.is_development,
            'call_state_store_ready': readiness['DATABASE_READY'],
            'admin_enabled': admin_enabled,
            'live_configuration_errors': len(settings.resolve().errors),
            'readiness': summary if settings.twilio_enabled else None,
            'clinic_phone': CLINIC['phone']}


# Outbound calls requested from the web client ------------------------------------------------------
@app.post('/api/calls/request')
async def call_request(r: CallRequest, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    if not r.consent:
        raise HTTPException(400, 'consent_required')
    destination = normalize_e164(r.patient_contact, settings.default_country_code)
    if not destination:
        raise HTTPException(422, 'patient_contact_must_be_a_valid_phone_number')
    if not settings.twilio_enabled:
        item = await asyncio.to_thread(runtime.repo.add_callback, source='web_call_request', contact=destination,
                                       topic=f'{r.name}: {r.topic}', preferred_window=r.preferred_window)
        return {'request_id': item['id'], 'status': 'queued', 'mode': 'demo',
                'message': 'Your call request has been received.'}
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, 'idempotency_key_required')
    cfg = settings.resolve()
    if cfg.errors:
        raise HTTPException(503, {'error': 'configuration_invalid', 'errors': cfg.errors})
    if cfg.allowed_destinations and destination not in cfg.allowed_destinations:
        raise HTTPException(403, {'error': 'destination_not_allowed'})
    await asyncio.to_thread(runtime.repo.record_consent, destination, source='web_form')
    readiness = await live_call_readiness(runtime, destination, verify_remote=True, max_age=0)
    if not readiness['LIVE_CALL_ALLOWED']:
        blockers = readiness['BLOCKERS']
        if any('DESTINATION_ON_DO_NOT_CALL_LIST' in blocker for blocker in blockers):
            raise HTTPException(403, {'error': 'do_not_call'})
        if any('DESTINATION_OPTED_OUT' in blocker for blocker in blockers):
            raise HTTPException(403, {'error': 'opted_out'})
        if any('QUOTA_EXHAUSTED' in blocker for blocker in blockers):
            raise HTTPException(429, {'error': 'quota_exhausted'})
        if any('CONCURRENCY_LIMIT_REACHED' in blocker for blocker in blockers):
            raise HTTPException(429, {'error': 'capacity_reached'})
        raise HTTPException(503, {'error': 'live_call_not_authorized', 'blockers': readiness['BLOCKERS']})
    try:
        call, created, replayed = await runtime.telephony.request_outbound(
            destination, source='web_form', requested_by='web', record_consent_source='web_form',
            idempotency_key=idempotency_key,
            request_name=r.name, request_topic=r.topic, preferred_window=r.preferred_window)
    except TwilioError as exc:
        logger.warning('web_call_request_rejected error=%s stage=%s twilio_code=%s request_id=%s to=%s', exc.error,
                       exc.details.get('stage', 'application'), exc.details.get('twilio_code'),
                       exc.details.get('twilio_request_id'), mask_phone(destination))
        raise HTTPException(exc.status_code, {'error': exc.error, **exc.details}) from exc
    return {'request_id': call.id, 'call_id': created.sid or None, 'status': call.status, 'replayed': replayed,
            'mode': 'live', 'message': 'Your call request has been received.'}


@app.get('/api/calls/{request_id}')
async def call_request_status(request_id: str):
    call = await asyncio.to_thread(runtime.repo.get_call, request_id)
    if not call:
        raise HTTPException(404, 'call_request_not_found')
    return {'request_id': call.id, 'status': call.status, 'preferred_window': call.preferred_window,
            'mode': 'live', 'call_id': call.provider_call_id[:8] + '...' if call.provider_call_id else None}


@app.get('/api/calls/{request_id}/events')
async def call_request_events(request_id: str, _admin: str = Depends(require_admin)):
    call = await asyncio.to_thread(runtime.repo.get_call, request_id)
    if not call:
        raise HTTPException(404, 'call_request_not_found')
    data = call.as_dict()
    data['customer_number'] = mask_phone(call.customer_number)
    return {'call': data, 'events': await asyncio.to_thread(runtime.repo.events, call.id, 500),
            'turns': await asyncio.to_thread(runtime.repo.turns, call.id)}


@app.post('/api/calls/{request_id}/hangup')
async def call_request_hangup(request_id: str, _admin: str = Depends(require_admin)):
    call = await asyncio.to_thread(runtime.repo.get_call, request_id)
    if not call:
        raise HTTPException(404, 'call_request_not_found')
    try:
        await runtime.telephony.hangup(call)
        sent = bool(call.provider_call_id)
    except Exception as exc:  # report, never leak provider details
        logger.warning('admin_hangup_failed error=%s', type(exc).__name__)
        sent = False
    return {'request_id': call.id, 'hangup_sent': sent}


# Admin ---------------------------------------------------------------------------------------------
@app.post('/api/admin/login')
def admin_login(r: AdminLoginRequest):
    enabled, reason = admin_status()
    if not enabled:
        raise HTTPException(503, {'error': 'admin_disabled', 'reason': reason})
    if not check_login(r.email, r.password):
        raise HTTPException(401, 'invalid_credentials')
    repo.log('admin_login', {'role': 'admin'})
    return {'access_token': session_token(), 'token_type': 'bearer', 'role': 'admin'}


@app.get('/api/usage')
async def usage():
    quota = await asyncio.to_thread(runtime.repo.quota_usage, runtime.telephony.policy())
    data = quota.as_dict()
    return {**data, 'threshold': threshold_label(data['percent_used']),
            'outbound_allowed': quota.remaining_seconds >= settings.twilio_max_call_seconds}


@app.get('/api/admin/config')
def get_config(_admin: str = Depends(require_admin)):
    return {'clinic': CLINIC, 'services': SERVICES,
            'configuration_gaps': ['live scheduling source', 'approved provider roster', 'pricing', 'consent wording',
                                   'notification provider'],
            'roles': ['receptionist', 'manager', 'admin', 'developer']}


@app.put('/api/admin/config')
def put_config(_admin: str = Depends(require_admin)):
    return get_config(_admin)


@app.get('/api/admin/activity')
async def activity(_admin: str = Depends(require_admin)):
    calls = await asyncio.to_thread(runtime.repo.recent_calls, 50)
    return {'events': await asyncio.to_thread(runtime.repo.events, None, 100),
            'calls': [c.as_dict() | {'customer_number': mask_phone(c.customer_number)} for c in calls],
            'callbacks': await asyncio.to_thread(runtime.repo.callbacks),
            'appointment_requests': await asyncio.to_thread(runtime.repo.appointment_requests),
            'bookings': [{'reference': k, 'status': v.status.value} for k, v in repo.bookings.items()]}


def _phone_or_422(value: str) -> str:
    phone = normalize_e164(value, settings.default_country_code)
    if not phone:
        raise HTTPException(422, 'invalid_phone_number')
    return phone


@app.post('/api/admin/consent')
async def record_consent(r: ConsentRequest, _admin: str = Depends(require_admin)):
    phone = _phone_or_422(r.phone_number)
    await asyncio.to_thread(runtime.repo.record_consent, phone, source=r.source, note=r.note)
    return {'phone_number': mask_phone(phone), 'consent': 'granted', 'source': r.source}


@app.delete('/api/admin/consent/{phone_number}')
async def revoke_consent(phone_number: str, _admin: str = Depends(require_admin)):
    phone = _phone_or_422(phone_number)
    await asyncio.to_thread(runtime.repo.revoke_consent, phone, source='admin')
    return {'phone_number': mask_phone(phone), 'consent': 'revoked'}


@app.post('/api/admin/dnc')
async def add_dnc(r: DncRequest, _admin: str = Depends(require_admin)):
    phone = _phone_or_422(r.phone_number)
    await asyncio.to_thread(runtime.repo.add_dnc, phone, reason=r.reason, source='admin')
    return {'phone_number': mask_phone(phone), 'do_not_call': True}


@app.delete('/api/admin/dnc/{phone_number}')
async def remove_dnc(phone_number: str, _admin: str = Depends(require_admin)):
    phone = _phone_or_422(phone_number)
    return {'phone_number': mask_phone(phone), 'removed': await asyncio.to_thread(runtime.repo.remove_dnc, phone)}


@app.post('/api/admin/opt-outs/clear')
async def clear_opt_out(r: OptOutClearRequest, _admin: str = Depends(require_admin)):
    """Only with a documented, lawful reason (for example new written consent)."""
    phone = _phone_or_422(r.phone_number)
    cleared = await asyncio.to_thread(runtime.repo.clear_opt_out, phone, reason=r.reason)
    return {'phone_number': mask_phone(phone), 'cleared': cleared}


@app.get('/api/admin/compliance/{phone_number}')
async def compliance(phone_number: str, _admin: str = Depends(require_admin)):
    phone = _phone_or_422(phone_number)
    return {'phone_number': mask_phone(phone), **await asyncio.to_thread(runtime.repo.compliance_status, phone)}
