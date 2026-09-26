"""Live-call API: preflight gating and Asterisk origination through /api/calls/request."""
import logging

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services.call_store import CallStore
from backend.app.telephony.asterisk import AsteriskTelephonyProvider
from backend.app.telephony.base import TelephonyError
from backend.app.telephony.host import HostReport
from fake_asterisk import FakeAMI
from test_receptionist_core import DEST, fake_pack

CALL = {'name': 'Live Fish Speech Reference Voice Test', 'patient_contact': DEST, 'preferred_window': 'Now',
        'topic': 'Mouth Care Solutions live AI receptionist test', 'consent': True}


@pytest.fixture
def live(monkeypatch):
    for key, value in dict(mock_mode=False, app_mode='live', call_provider='asterisk', asterisk_ami_secret='test-secret',
                           outbound_allowed_destinations=DEST, fish_speech_enabled=True, telephony_interface='',
                           outbound_caller_id='', outbound_rate_limit_seconds=300).items():
        monkeypatch.setattr(main.settings, key, value)
    store = CallStore(':memory:')
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main.provider, 'store', store)
    monkeypatch.setattr(main, '_fish_ready', lambda: True)
    monkeypatch.setattr(main, 'voice_pack', fake_pack)
    monkeypatch.setattr(main.agi_server, 'status', 'listening')
    monkeypatch.setattr(main.transcriber, 'status', 'ready')
    return store


def allow(monkeypatch, allowed=True, blockers=()):
    monkeypatch.setattr(main, '_telephony_preflight', lambda destination, refresh=False: {
        'LIVE_CALL_ALLOWED': allowed, 'BLOCKERS': list(blockers)})


def test_demo_preflight_never_allows_live_calls():
    body = TestClient(main.app).get('/api/telephony/preflight').json()
    assert body['MOCK_MODE'] is True and body['LIVE_CALL_ALLOWED'] is False and body['SOFTWARE_READY_FOR_LIVE_CALL'] is False
    assert 'MOCK_MODE=true: demo mode never places real calls' in body['BLOCKERS']
    for key in ('ASTERISK_INSTALLED', 'ASTERISK_RUNNING', 'ASTERISK_VERSION', 'ASTERISK_CONTROL_READY', 'ASTERISK_DIALPLAN_READY',
                'TELEPHONY_INTERFACE_READY', 'GSM_MODEM_PRESENT', 'GSM_REGISTERED', 'SIM_READY', 'SIP_TRUNK_READY',
                'OUTBOUND_CHANNEL_READY', 'FISH_SPEECH_READY', 'VOICE_PACK_COMPLETE', 'VOICE_PACK_VALID',
                'VOICE_PACK_CHECKSUM_VALID', 'PACKAGED_AUDIO_RUNTIME_READY', 'CALL_STATE_STORE_READY',
                'SOFTWARE_READY_FOR_LIVE_CALL', 'LIVE_CALL_ALLOWED', 'BLOCKERS'):
        assert key in body, key


def test_preflight_rejects_non_e164_destination():
    assert TestClient(main.app).get('/api/telephony/preflight', params={'destination': '9908552414'}).status_code == 422


def _real_preflight(monkeypatch, ami, **settings):
    for key, value in settings.items():
        monkeypatch.setattr(main.settings, key, value)
    monkeypatch.setattr(main.settings, 'asterisk_ami_port', ami.port)
    installed = lambda distro, refresh=False: HostReport(wsl_installed=True, distro_present=True, asterisk_installed=True,
                                                          asterisk_version='Asterisk 20.6.0')
    monkeypatch.setattr(main, 'provider', AsteriskTelephonyProvider(main.settings, main.store, host_inspector=installed))
    return TestClient(main.app).get('/api/telephony/preflight', params={'refresh': '1'}).json()


def test_asterisk_without_a_phone_line_is_software_ready_but_live_calls_stay_blocked(live, monkeypatch):
    ami = FakeAMI()
    try:
        body = _real_preflight(monkeypatch, ami)
    finally:
        ami.close()
    assert body['ASTERISK_RUNNING'] and body['ASTERISK_CONTROL_READY'] and body['ASTERISK_DIALPLAN_READY']
    assert body['PACKAGED_AUDIO_RUNTIME_READY'] and body['VOICE_PACK_VALID'] and body['FISH_SPEECH_READY']
    assert body['SOFTWARE_READY_FOR_LIVE_CALL'] is True and body['SOFTWARE_BLOCKERS'] == []
    assert body['LIVE_CALL_ALLOWED'] is False
    assert body['INTERFACE_BLOCKERS'][0].startswith('NO_EXTERNAL_TELEPHONY_INTERFACE')


def test_registered_trunk_and_ready_software_allow_the_call(live, monkeypatch):
    ami = FakeAMI(registrations=[{'ObjectName': 'pstn-trunk-reg', 'Status': 'Registered'}])
    try:
        body = _real_preflight(monkeypatch, ami, telephony_interface='sip', sip_trunk_host='sip.example.net',
                               sip_trunk_username='clinic', sip_trunk_password='trunk-pass', outbound_caller_id='+914000000000')
    finally:
        ami.close()
    assert body['BLOCKERS'] == [] and body['LIVE_CALL_ALLOWED'] is True and body['OUTBOUND_CHANNEL_READY'] is True
    assert 'trunk-pass' not in str(body) and 'test-secret' not in str(body)


@pytest.mark.parametrize('patch,blocker', [
    ({'_fish_ready': lambda: False}, 'FISH_SPEECH_NOT_REACHABLE'),
])
def test_software_problems_block_even_with_a_line(live, monkeypatch, patch, blocker):
    for name, value in patch.items():
        monkeypatch.setattr(main, name, value)
    ami = FakeAMI(registrations=[{'ObjectName': 'pstn-trunk-reg', 'Status': 'Registered'}])
    try:
        body = _real_preflight(monkeypatch, ami, telephony_interface='sip', sip_trunk_host='sip.example.net',
                               sip_trunk_username='clinic', sip_trunk_password='p', outbound_caller_id='+914000000000')
    finally:
        ami.close()
    assert body['SOFTWARE_READY_FOR_LIVE_CALL'] is False and body['LIVE_CALL_ALLOWED'] is False
    assert blocker in body['SOFTWARE_BLOCKERS']


def test_agi_and_asr_readiness_are_required(live, monkeypatch):
    monkeypatch.setattr(main.agi_server, 'status', 'error')
    monkeypatch.setattr(main.transcriber, 'status', 'loading')
    body = main._telephony_preflight(DEST)
    assert any(b.startswith('AGI_SERVER_NOT_LISTENING') for b in body['SOFTWARE_BLOCKERS'])
    assert any(b.startswith('ASR_NOT_READY') for b in body['SOFTWARE_BLOCKERS'])


def test_live_call_originates_through_asterisk(live, monkeypatch):
    allow(monkeypatch)
    captured = []
    monkeypatch.setattr(main.provider, 'create_outbound_call',
                        lambda call_id, destination: captured.append((call_id, destination)) or {'call_id': call_id, 'status': 'queued', 'channel': 'PJSIP/+91******0001@pstn-trunk'})
    body = TestClient(main.app).post('/api/calls/request', json=CALL).json()
    assert body['mode'] == 'live' and body['status'] == 'queued' and captured == [(body['request_id'], DEST)]
    record = live.get_call_request(body['request_id'])
    assert record['call_id'] == body['call_id'] == body['request_id']
    assert [e['kind'] for e in live.events(body['request_id'])] == ['call_originated']


def test_live_call_is_rate_limited_and_blocked_calls_place_nothing(live, monkeypatch):
    allow(monkeypatch)
    calls = []
    monkeypatch.setattr(main.provider, 'create_outbound_call', lambda call_id, destination: calls.append(call_id) or {'call_id': call_id, 'status': 'queued'})
    client = TestClient(main.app)
    assert client.post('/api/calls/request', json=CALL).status_code == 200
    assert client.post('/api/calls/request', json=CALL).status_code == 429
    assert len(calls) == 1


def test_preflight_blockers_stop_the_call(live, monkeypatch):
    allow(monkeypatch, allowed=False, blockers=['NO_EXTERNAL_TELEPHONY_INTERFACE: no GSM/4G modem detected'])
    monkeypatch.setattr(main.provider, 'create_outbound_call', lambda *a: pytest.fail('must not originate'))
    response = TestClient(main.app).post('/api/calls/request', json=CALL)
    assert response.status_code == 503
    detail = response.json()['detail']
    assert detail['error'] == 'live_call_preflight_failed' and detail['blockers'][0].startswith('NO_EXTERNAL')
    assert live.get_call_request(detail['request_id'])['status'] == 'blocked'


def test_non_allowlisted_destination_is_refused_before_any_check(live, monkeypatch):
    monkeypatch.setattr(main, '_telephony_preflight', lambda *a, **k: pytest.fail('preflight not needed'))
    assert TestClient(main.app).post('/api/calls/request', json={**CALL, 'patient_contact': '+919811111111'}).status_code == 403


def test_provider_failure_is_reported_with_stage_and_correlation_id(live, monkeypatch, caplog):
    allow(monkeypatch)

    def reject(call_id, destination):
        raise TelephonyError('asterisk_originate', 'Extension does not exist.')
    monkeypatch.setattr(main.provider, 'create_outbound_call', reject)
    with caplog.at_level(logging.ERROR, logger='backend.app.main'):
        response = TestClient(main.app).post('/api/calls/request', json=CALL)
    detail = response.json()['detail']
    assert response.status_code == 502 and detail['stage'] == 'asterisk_originate'
    assert f"correlation_id={detail['request_id']}" in caplog.text and DEST not in caplog.text
    assert live.get_call_request(detail['request_id'])['status'] == 'failed'


def test_invalid_live_configuration_refuses_calls(live, monkeypatch):
    monkeypatch.setattr(main.settings, 'call_provider', 'mock')
    assert TestClient(main.app).post('/api/calls/request', json=CALL).status_code == 503


def test_call_request_requires_valid_phone_and_consent():
    client = TestClient(main.app)
    assert client.post('/api/calls/request', json={**CALL, 'patient_contact': 'not-a-number'}).status_code == 422
    assert client.post('/api/calls/request', json={**CALL, 'patient_contact': '+९१९९०८५५२४१४'}).status_code == 422
    assert client.post('/api/calls/request', json={**CALL, 'consent': False}).status_code == 400


def test_call_evidence_and_hangup_are_admin_only(live):
    request = live.create_call_request(name='t', destination=DEST, preferred_window='Now', topic='t', mode='live')
    client = TestClient(main.app)
    assert client.get(f"/api/calls/{request['request_id']}/events").status_code == 403
    assert client.post(f"/api/calls/{request['request_id']}/hangup").status_code == 403
    assert client.get(f"/api/calls/{request['request_id']}").json()['status'] == 'pending'
