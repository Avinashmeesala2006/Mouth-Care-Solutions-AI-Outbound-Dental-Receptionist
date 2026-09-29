"""Twilio telephony: account policy, diagnostic preflight, call requests that reach Twilio after the
application's safety checks, signed webhooks, Fish-only TwiML,
grounded conversation over <Gather>, idempotent status callbacks and the REST client.
No test contacts Twilio or places a call (fake account + httpx.MockTransport)."""
import asyncio
import re
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import FROM_NUMBER, OTHER, PATIENT, PUBLIC, FakeTwilioClient
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.db.memory import InMemoryCallRepository
from backend.app.telephony.twilio import TwilioError, TwilioRestClient, country_of, twilio_signature
from backend.app.voice.fish_speech import FishHealth


class PostgresBackedMemoryRepo(InMemoryCallRepository):
    """Gate logic only: real PostgreSQL behaviour is covered by test_repository_contract."""
    backend = 'postgresql'


def fish_ready():
    return FishHealth(enabled=True, configured=True, reachable=True, model_loaded=True, ready=True, reference_ready=True,
                      latency_ms=5, error=None, model='fish-speech-1.5', version='v1.5.1', private_endpoint=True)


def public_transport(instance_id, *, webhook_status=403):
    def handler(request: httpx.Request):
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'instance_id': instance_id})
        if request.url.path == '/api/telephony/twilio/status':
            return httpx.Response(webhook_status)
        if request.url.path.startswith('/api/telephony/audio/'):
            return httpx.Response(200, headers={'content-type': 'audio/wav'})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def ready(make_runtime, monkeypatch):
    """Everything ready except what a test changes."""
    def build(client=None, repo=None, **overrides):
        rt = make_runtime(client=client or FakeTwilioClient(), repo=repo or PostgresBackedMemoryRepo(), **overrides)
        rt.instance_id = 'instance-under-test'
        rt.telephony.public_transport = public_transport(rt.instance_id)

        async def fish(*, max_age=0):
            return fish_ready()
        monkeypatch.setattr(rt, 'fish_health', fish)
        rt.repo.record_consent(PATIENT, source='test')
        return rt
    return build


def signed(path_and_query: str, params: dict, token: str = 't' * 32) -> dict:
    return {'X-Twilio-Signature': twilio_signature(PUBLIC + path_and_query, params, token)}


def preflight(rt, destination=PATIENT, verify_remote=True):
    from backend.app.api.readiness import live_call_readiness
    return asyncio.run(live_call_readiness(rt, destination, verify_remote=verify_remote, max_age=0))


# Routes and signatures -----------------------------------------------------------------------------
def test_twilio_routes_are_active_and_old_provider_routes_are_absent(make_runtime):
    make_runtime()
    paths = {route.path for route in main.app.routes}
    assert {'/api/telephony/twilio/outbound', '/api/telephony/twilio/gather', '/api/telephony/twilio/status',
            '/api/twilio/preflight'} <= paths
    assert not any(word in path.lower() for path in paths for word in ('vonage', 'asterisk', 'elevenlabs'))


def test_signatures_are_required_on_every_webhook(make_runtime):
    make_runtime()
    client = TestClient(main.app)
    body = {'CallStatus': 'ringing', 'CallSid': 'CA' + '9' * 32}
    assert client.post('/api/telephony/twilio/status', data=body).status_code == 403
    assert client.post('/api/telephony/twilio/status', data=body,
                       headers=signed('/api/telephony/twilio/status', body)).status_code == 200
    tampered = {**body, 'CallStatus': 'completed'}
    assert client.post('/api/telephony/twilio/status', data=tampered,
                       headers=signed('/api/telephony/twilio/status', body)).status_code == 403
    assert client.post('/api/telephony/twilio/status?session_id=x', data=body,
                       headers=signed('/api/telephony/twilio/status', body)).status_code == 403
    assert client.post('/api/telephony/twilio/status', data=body,
                       headers=signed('/api/telephony/twilio/status', body, token='w' * 32)).status_code == 403


def test_disabled_signature_validation_never_accepts_webhooks(make_runtime):
    make_runtime(twilio_validate_signature=False)
    assert TestClient(main.app).post('/api/telephony/twilio/status', data={'CallStatus': 'ringing'}).status_code == 503


# Account policy -----------------------------------------------------------------------------------------
def test_full_account_with_owned_voice_number_is_authorized(ready):
    rt = ready()
    account = asyncio.run(rt.telephony.account_check(PATIENT))
    assert account['TWILIO_AUTHENTICATED'] and account['ACCOUNT_TYPE'] == 'Full' and account['ACCOUNT_STATUS'] == 'active'
    assert account['OWNED_TWILIO_NUMBER_COUNT'] == 1 and account['FROM_OWNED'] and account['FROM_VOICE_CAPABLE']
    assert account['FROM_AUTHORIZED'] and account['DESTINATION_AUTHORIZED'] and account['CUSTOM_WEBHOOK_CALL_ALLOWED']
    assert account['TRIAL_RESTRICTION'] is False and account['ACCOUNT_BLOCKERS'] == []


def test_trial_account_never_allows_the_application_webhook_call(ready):
    rt = ready(client=FakeTwilioClient(account_type='Trial', verified=(PATIENT,)))
    account = asyncio.run(rt.telephony.account_check(PATIENT))
    assert account['TRIAL_RESTRICTION'] is True and account['TRIAL_CALL_ALLOWED'] is True
    assert account['CUSTOM_WEBHOOK_CALL_ALLOWED'] is False
    assert any(b.startswith('TWILIO_TRIAL_RESTRICTION') for b in account['ACCOUNT_BLOCKERS'])
    assert preflight(rt)['LIVE_CALL_ALLOWED'] is False


def test_trial_destination_must_be_a_verified_caller_id(ready):
    rt = ready(client=FakeTwilioClient(account_type='Trial', verified=()))
    account = asyncio.run(rt.telephony.account_check(PATIENT))
    assert account['DESTINATION_AUTHORIZED'] is False and account['TRIAL_CALL_ALLOWED'] is False
    assert any('Verified Caller IDs' in b for b in account['ACCOUNT_BLOCKERS'])


@pytest.mark.parametrize('client,flag,blocker', [
    (FakeTwilioClient(owned=(OTHER,)), 'FROM_AUTHORIZED', 'FROM_NOT_AUTHORIZED'),
    (FakeTwilioClient(voice=False), 'FROM_AUTHORIZED', 'FROM_NOT_AUTHORIZED'),
    (FakeTwilioClient(owned=()), 'FROM_AUTHORIZED', 'NO_TWILIO_NUMBERS_OWNED'),
    (FakeTwilioClient(country_permission=False), 'DESTINATION_AUTHORIZED', 'DESTINATION_COUNTRY_NOT_PERMITTED'),
    (FakeTwilioClient(authenticated=False), 'TWILIO_AUTHENTICATED', 'TWILIO_AUTHENTICATION_FAILED'),
    (FakeTwilioClient(status='suspended'), 'CUSTOM_WEBHOOK_CALL_ALLOWED', 'TWILIO_ACCOUNT_NOT_ACTIVE'),
])
def test_account_problems_are_reported_not_faked(ready, client, flag, blocker):
    rt = ready(client=client)
    account = asyncio.run(rt.telephony.account_check(PATIENT))
    assert account[flag] is False
    assert any(b.startswith(blocker) for b in account['ACCOUNT_BLOCKERS'])
    assert preflight(rt)['LIVE_CALL_ALLOWED'] is False


def test_missing_credentials_are_reported_without_any_request(ready):
    rt = ready(twilio_account_sid='', twilio_auth_token='')
    account = asyncio.run(rt.telephony.account_check(PATIENT))
    assert account['TWILIO_AUTHENTICATED'] is False and account['ACCOUNT_BLOCKERS'] == ['TWILIO_CREDENTIALS_NOT_CONFIGURED']
    assert rt.telephony.client.created == []


def test_destination_country_mapping():
    assert country_of('+919000000000') == 'IN' and country_of('+12025550143') == 'US' and country_of('+9715000') == 'AE'


# Diagnostic preflight ---------------------------------------------------------------------------------------
def test_live_call_allowed_only_when_every_gate_passes(ready):
    rt = ready()
    body = preflight(rt)
    assert body['LIVE_CALL_ALLOWED'] is True, body['BLOCKERS']
    for key in ('TWILIO_AUTHENTICATED', 'FROM_AUTHORIZED', 'DESTINATION_AUTHORIZED', 'CUSTOM_WEBHOOK_CALL_ALLOWED',
                'PUBLIC_HEALTH', 'PUBLIC_WEBHOOK_REACHABLE', 'TWILIO_SIGNATURE_VALIDATION_READY', 'FISH_SPEECH_READY',
                'VOICE_PACK_COMPLETE', 'VOICE_PACK_VALID', 'VOICE_PACK_CHECKSUM_VALID', 'PACKAGED_AUDIO_RUNTIME_READY',
                'POSTGRES_READY', 'SOFTWARE_READY_FOR_LIVE_CALL'):
        assert body[key] is True, key
    assert body['VOICE_PACK_ASSET_COUNT'] == 25 and body['DESTINATION_COUNTRY'] == 'US'
    assert preflight(rt, verify_remote=False)['LIVE_CALL_ALLOWED'] is False


@pytest.mark.parametrize('breaker', ['memory_db', 'fish_down', 'public_other_process', 'webhook_accepts_unsigned',
                                     'no_consent', 'opted_out'])
def test_each_failing_condition_is_reported_by_preflight(ready, monkeypatch, breaker):
    rt = ready(repo=InMemoryCallRepository() if breaker == 'memory_db' else None)
    rt.repo.record_consent(PATIENT, source='test')
    if breaker == 'fish_down':
        async def down(*, max_age=0):
            return FishHealth(**{**fish_ready().__dict__, 'ready': False, 'reachable': False, 'error': 'connection_refused'})
        monkeypatch.setattr(rt, 'fish_health', down)
    if breaker == 'public_other_process':
        rt.telephony.public_transport = public_transport('some-other-process')
    if breaker == 'webhook_accepts_unsigned':
        rt.telephony.public_transport = public_transport(rt.instance_id, webhook_status=200)
    if breaker == 'no_consent':
        rt.repo.revoke_consent(PATIENT, source='test')
    if breaker == 'opted_out':
        rt.repo.record_opt_out(PATIENT, source='voice')
    body = preflight(rt)
    assert body['LIVE_CALL_ALLOWED'] is False and body['BLOCKERS']


def test_preflight_endpoint_is_public_and_never_leaks_secrets(ready):
    rt = ready()
    response = TestClient(main.app).get('/api/twilio/preflight', params={'destination': PATIENT})
    assert response.status_code == 200 and response.json()['LIVE_CALL_ALLOWED'] is False   # no verify_remote
    text = response.text
    assert rt.settings.twilio_auth_token not in text and rt.settings.twilio_account_sid not in text and PATIENT not in text


def test_live_status_and_ready_endpoints_answer(ready):
    rt = ready()
    client = TestClient(main.app)
    status = client.get('/api/status')
    assert status.status_code == 200
    body = status.json()
    assert body['mode'] == 'live' and body['telephony'] == 'twilio' and body['call_state_store_ready'] is True
    assert body['readiness']['LIVE_CALL_ALLOWED'] is False and rt.settings.twilio_auth_token not in status.text
    assert client.get('/ready').status_code in (200, 503)


# Call request path -------------------------------------------------------------------------------------------
REQUEST = {'name': 'QA Patient', 'patient_contact': PATIENT, 'preferred_window': 'Now', 'topic': 'Appointment', 'consent': True}


def test_call_request_places_exactly_one_call(ready):
    rt = ready()
    response = TestClient(main.app).post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-one'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['call_id'].startswith('CA') and body['status'] == 'ORIGINATE_ACCEPTED'
    (created,) = rt.telephony.client.created
    assert created['to'] == PATIENT and created['from_number'] == FROM_NUMBER
    assert created['url'] == f"{PUBLIC}/api/telephony/twilio/outbound?session_id={body['request_id']}"
    assert created['status_callback'] == f"{PUBLIC}/api/telephony/twilio/status?session_id={body['request_id']}"


def test_preflight_false_blocks_the_request_before_twilio(ready, monkeypatch):
    rt = ready(client=FakeTwilioClient(account_type='Trial', owned=(), verified=()))

    async def down(*, max_age=0):
        return FishHealth(**{**fish_ready().__dict__, 'ready': False, 'reachable': False, 'error': 'connection_refused'})
    monkeypatch.setattr(rt, 'fish_health', down)
    before = preflight(rt)
    assert before['LIVE_CALL_ALLOWED'] is False and before['BLOCKERS']      # the report stays truthful
    response = TestClient(main.app).post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-trial'})
    assert response.status_code == 503, response.text
    assert response.json()['detail']['error'] == 'live_call_not_authorized'
    assert rt.telephony.client.created == []
    assert preflight(rt)['LIVE_CALL_ALLOWED'] is False


@pytest.mark.parametrize('case,status,error', [
    ('no_consent_in_form', 400, 'consent_required'),
    ('allow_list', 403, 'destination_not_allowed'),
    ('do_not_call', 403, 'do_not_call'),
    ('opted_out', 403, 'opted_out'),
    ('quota', 429, 'quota_exhausted'),
    ('invalid_phone', 422, 'patient_contact_must_be_a_valid_phone_number'),
    ('no_auth_token', 503, 'configuration_invalid'),
    ('no_account_sid', 503, 'configuration_invalid'),
])
def test_application_safety_checks_still_block_before_twilio(ready, case, status, error):
    overrides = {'allow_list': {'outbound_allowed_destinations': OTHER},
                 'quota': {'service_quota_seconds': 60},
                 'no_auth_token': {'twilio_auth_token': ''},
                 'no_account_sid': {'twilio_account_sid': ''}}.get(case, {})
    rt = ready(**overrides)
    if case == 'do_not_call':
        rt.repo.add_dnc(PATIENT, reason='asked', source='test')
    if case == 'opted_out':
        rt.repo.record_opt_out(PATIENT, source='voice')
    payload = {**REQUEST, **({'consent': False} if case == 'no_consent_in_form' else {}),
               **({'patient_contact': 'call me maybe'} if case == 'invalid_phone' else {})}
    response = TestClient(main.app).post('/api/calls/request', json=payload, headers={'Idempotency-Key': 'request-safety'})
    detail = response.json()['detail']
    assert response.status_code == status, response.text
    assert (detail['error'] if isinstance(detail, dict) else detail) == error
    assert rt.telephony.client.created == []


def test_stored_consent_is_required_by_the_reservation(ready):
    rt = ready()
    rt.repo.revoke_consent(PATIENT, source='test')
    with pytest.raises(TwilioError) as info:
        asyncio.run(rt.telephony.request_outbound(PATIENT, source='test', requested_by='t'))
    assert info.value.status_code == 403 and info.value.error == 'consent_missing'
    assert rt.telephony.client.created == []


def test_rate_limit_still_blocks_a_second_request(ready):
    rt = ready()
    client = TestClient(main.app)
    assert client.post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-replay'}).status_code == 200
    again = client.post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-replay-second'})
    assert again.status_code == 429 and again.json()['detail']['error'] == 'rate_limited'
    assert len(rt.telephony.client.created) == 1


def test_api_idempotency_replays_without_a_second_provider_call(ready):
    rt = ready()
    client = TestClient(main.app)
    headers = {'Idempotency-Key': 'api-replay-0001'}
    first = client.post('/api/calls/request', json=REQUEST, headers=headers)
    second = client.post('/api/calls/request', json=REQUEST, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json()['replayed'] is True
    assert second.json()['request_id'] == first.json()['request_id']
    assert len(rt.telephony.client.created) == 1


def test_rejected_call_is_recorded_and_never_retried(ready):
    rt = ready(client=FakeTwilioClient(error=TwilioError(502, 'twilio_call_rejected', http_status=400, twilio_code=21219,
                                                        twilio_message='unverified number')))
    response = TestClient(main.app).post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-provider-error'})
    assert response.status_code == 502 and response.json()['detail']['twilio_code'] == 21219
    call = rt.repo.get_call(response.json()['detail']['session_id'])
    assert call.status == 'FAILED' and call.termination_reason == 'twilio_call_rejected'


def test_real_twilio_rejection_is_surfaced_with_sanitized_diagnostics(ready):
    """The real REST client against a Twilio-shaped 400: stage, status, code, request id; no secrets or full numbers."""
    requests_seen = []

    def handler(request: httpx.Request):
        requests_seen.append(request)
        if request.method == 'GET':
            if request.url.path.endswith('IncomingPhoneNumbers.json'):
                return httpx.Response(200, json={'incoming_phone_numbers': [
                    {'phone_number': FROM_NUMBER, 'capabilities': {'voice': True}}]})
            if request.url.path.endswith('OutgoingCallerIds.json'):
                return httpx.Response(200, json={'outgoing_caller_ids': []})
            if '/DialingPermissions/Countries/' in request.url.path:
                return httpx.Response(200, json={'low_risk_numbers_enabled': True})
            return httpx.Response(200, json={'status': 'active', 'type': 'Full'})
        return httpx.Response(400, headers={'Twilio-Request-Id': 'RQ' + 'a' * 32}, json={
            'code': 21219, 'status': 400, 'more_info': 'https://www.twilio.com/docs/errors/21219',
            'message': f"The number {PATIENT} is unverified. Trial accounts cannot place calls to unverified numbers "
                       f"(account {'AC' + '1' * 32})."})
    rt = ready()
    rt.telephony.client = TwilioRestClient(rt.settings.twilio_account_sid, rt.settings.twilio_auth_token,
                                           transport=httpx.MockTransport(handler))
    response = TestClient(main.app).post('/api/calls/request', json=REQUEST, headers={'Idempotency-Key': 'request-unknown'})
    assert response.status_code == 502 and sum(request.method == 'POST' for request in requests_seen) == 1  # one create attempt
    detail = response.json()['detail']
    assert detail['error'] == 'twilio_call_rejected' and detail['stage'] == 'twilio_create_call'
    assert detail['http_status'] == 400 and detail['twilio_code'] == 21219
    assert detail['twilio_request_id'] == 'RQ' + 'a' * 32
    assert detail['twilio_more_info'].endswith('/21219') and 'unverified' in detail['twilio_message']
    for secret in (PATIENT, rt.settings.twilio_auth_token, rt.settings.twilio_account_sid):
        assert secret not in response.text
    call = rt.repo.get_call(detail['session_id'])
    assert call.status == 'FAILED'
    (event,) = [e for e in rt.repo.events(call.id, 50) if e['kind'] == 'twilio_create_call_failed']
    assert event['data']['twilio_code'] == 21219 and event['data']['twilio_request_id'] == 'RQ' + 'a' * 32
    assert event['data']['stage'] == 'twilio_create_call' and PATIENT not in str(event['data'])


def test_idempotent_request_returns_the_same_call(ready):
    rt = ready()
    first = asyncio.run(rt.telephony.request_outbound(PATIENT, source='test', requested_by='t',
                                                      idempotency_key='idem-000001'))
    again = asyncio.run(rt.telephony.request_outbound(PATIENT, source='test', requested_by='t',
                                                      idempotency_key='idem-000001'))
    assert again[2] is True and again[0].id == first[0].id and len(rt.telephony.client.created) == 1


# TwiML and conversation ----------------------------------------------------------------------------------------
def dialed(rt):
    call, _created, _ = asyncio.run(rt.telephony.request_outbound(PATIENT, source='test', requested_by='t'))
    return call


def post(path, params):
    return TestClient(main.app).post(path, data=params, headers=signed(path, params))


def plays(xml):
    return re.findall(r'<Play>[^<]*/api/telephony/audio/([a-z_]+)</Play>', xml)


def test_answer_webhook_plays_fish_greeting_and_listens(ready):
    rt = ready()
    call = dialed(rt)
    path = f'/api/telephony/twilio/outbound?session_id={call.id}'
    response = post(path, {'CallSid': call.provider_call_id, 'CallStatus': 'in-progress'})
    xml = response.text
    assert response.status_code == 200 and response.headers['content-type'].startswith('application/xml')
    assert plays(xml) == ['outbound_greeting']
    assert '<Gather input="speech dtmf"' in xml and f'gather?session_id={call.id}' in xml
    assert '<Say' not in xml and rt.repo.get_call(call.id).status == 'ANSWERED'


def test_conversation_turns_use_the_grounded_engine_and_fish_assets(ready):
    rt = ready()
    call = dialed(rt)
    post(f'/api/telephony/twilio/outbound?session_id={call.id}', {'CallSid': call.provider_call_id})
    gather = f'/api/telephony/twilio/gather?session_id={call.id}'
    assert plays(post(gather, {'SpeechResult': 'What are your clinic hours?'}).text) == ['clinic_hours', 'anything_else']
    assert plays(post(gather, {'SpeechResult': 'What is your email address?'}).text) == ['clinic_email', 'anything_else']
    assert plays(post(gather, {'SpeechResult': ''}).text) == ['repeat_or_not_understood']
    final = post(gather, {'SpeechResult': 'No, that is all, thank you'}).text
    assert plays(final) == ['goodbye'] and '<Hangup/>' in final and '<Say' not in final
    assert [t['intent'] for t in rt.repo.turns(call.id)] == ['greeting', 'clinic_hours', 'clinic_email', 'silence', 'goodbye']
    assert rt.repo.get_call(call.id).status == 'ENDING'


def test_opt_out_during_a_call_is_recorded_and_confirmed(ready):
    rt = ready()
    call = dialed(rt)
    xml = post(f'/api/telephony/twilio/gather?session_id={call.id}', {'SpeechResult': 'Please do not call me again'}).text
    assert plays(xml) == ['opt_out_confirmed'] and '<Hangup/>' in xml
    assert rt.repo.compliance_status(PATIENT)['opted_out'] is True


def test_unknown_sessions_get_a_silent_hangup_not_twilio_error_speech(ready):
    ready()
    xml = post('/api/telephony/twilio/gather?session_id=00000000-0000-4000-8000-000000000000', {'SpeechResult': 'hi'}).text
    assert xml.endswith('<Response><Hangup/></Response>')


def test_inbound_call_creates_a_session(ready):
    rt = ready()
    sid = 'CA' + '7' * 32
    xml = post('/api/telephony/twilio/outbound', {'CallSid': sid, 'From': OTHER, 'To': FROM_NUMBER}).text
    assert plays(xml) == ['greeting']
    call = rt.repo.get_call_by_provider_id(sid)
    assert call.direction == 'inbound' and call.customer_number == OTHER


# Status callbacks ------------------------------------------------------------------------------------------------
def test_status_callbacks_are_idempotent_and_record_duration(ready):
    rt = ready()
    call = dialed(rt)
    path = f'/api/telephony/twilio/status?session_id={call.id}'
    base = {'CallSid': call.provider_call_id}
    for status, seq in (('initiated', '0'), ('ringing', '1'), ('in-progress', '2')):
        assert post(path, {**base, 'CallStatus': status, 'SequenceNumber': seq}).status_code == 200
    post(path, {**base, 'CallStatus': 'ringing', 'SequenceNumber': '1'})               # duplicate delivery
    post(path, {**base, 'CallStatus': 'completed', 'SequenceNumber': '3', 'CallDuration': '37'})
    final = rt.repo.get_call(call.id)
    assert final.status == 'COMPLETED' and final.duration_seconds == 37
    assert rt.repo.quota_usage(rt.telephony.policy()).consumed_seconds == 37
    assert sum(1 for e in rt.repo.events(call.id) if e['kind'] == 'twilio_status') == 4


def test_completed_without_answer_is_no_answer(ready):
    rt = ready()
    call = dialed(rt)
    post(f'/api/telephony/twilio/status?session_id={call.id}',
         {'CallSid': call.provider_call_id, 'CallStatus': 'completed', 'SequenceNumber': '1', 'CallDuration': '0'})
    assert rt.repo.get_call(call.id).status == 'NO_ANSWER'


# REST client ---------------------------------------------------------------------------------------------------
def test_rest_client_create_call_payload_and_single_attempt():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={'sid': 'CA' + '1' * 32, 'status': 'queued'})

    client = TwilioRestClient('AC' + '2' * 32, 's' * 32, transport=httpx.MockTransport(handler))
    call = asyncio.run(client.create_call(to=PATIENT, from_number=FROM_NUMBER, url=f'{PUBLIC}/o?session_id=1',
                                          status_callback=f'{PUBLIC}/s?session_id=1'))
    assert call.sid.startswith('CA') and len(seen) == 1
    form = parse_qs(seen[0].content.decode())
    assert form['To'] == [PATIENT] and form['From'] == [FROM_NUMBER] and form['Method'] == ['POST']
    assert form['StatusCallbackEvent'] == ['initiated', 'ringing', 'answered', 'completed']
    assert seen[0].headers['authorization'].startswith('Basic ')


@pytest.mark.parametrize('handler,error', [
    (lambda r: httpx.Response(400, json={'code': 21219, 'message': 'unverified'}), 'twilio_call_rejected'),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('slow', request=r)), 'twilio_outcome_unknown'),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError('down', request=r)), 'twilio_unreachable'),
])
def test_rest_client_errors_are_classified(handler, error):
    client = TwilioRestClient('AC' + '2' * 32, 's' * 32, transport=httpx.MockTransport(handler))
    with pytest.raises(TwilioError) as info:
        asyncio.run(client.create_call(to=PATIENT, from_number=FROM_NUMBER, url='u', status_callback='s'))
    assert info.value.error == error and 's' * 32 not in str(info.value.details)


def test_rest_client_reads_inventory_across_pages_and_hangs_up():
    def handler(request):
        path = request.url.path
        if path.endswith('IncomingPhoneNumbers.json'):
            if 'Page=1' in str(request.url):
                return httpx.Response(200, json={'incoming_phone_numbers': [{'phone_number': OTHER}], 'next_page_uri': None})
            return httpx.Response(200, json={'incoming_phone_numbers': [{'phone_number': FROM_NUMBER}],
                                             'next_page_uri': '/2010-04-01/Accounts/AC/IncomingPhoneNumbers.json?Page=1'})
        if path.endswith('.json') and '/Calls/' in path:
            assert parse_qs(request.content.decode()) == {'Status': ['completed']}
            return httpx.Response(200, json={'status': 'completed'})
        return httpx.Response(200, json={'status': 'active', 'type': 'Full'})

    client = TwilioRestClient('AC' + '2' * 32, 's' * 32, transport=httpx.MockTransport(handler))
    numbers = asyncio.run(client.incoming_numbers())
    assert [n['phone_number'] for n in numbers] == [FROM_NUMBER, OTHER]
    assert asyncio.run(client.hangup('CA' + '3' * 32)) is True
