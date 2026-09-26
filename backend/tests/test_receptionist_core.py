"""Receptionist core: truthful configuration, grounded phone conversation, verified voice
pack, fail-closed booking, admin lock-down and cached readiness."""
import hashlib
import io
import json
import math
import random
import wave

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.core.config import Settings, to_wsl_path
from backend.app.services import phone_agent
from backend.app.services.call_store import CallStore
from backend.app.services.voice_pack import (GENERATION_STATUS, VALIDATION_STATUS, VoiceAsset, VoicePackStatus,
                                             load_voice_pack, speech_metrics)

DEST = '+919900000001'


def wav(samples, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b''.join(int(max(-32767, min(32767, s))).to_bytes(2, 'little', signed=True) for s in samples))
    return buffer.getvalue()


def tone(seconds=1.2, rate=8000):
    return wav([12000 * math.sin(2 * math.pi * 480 * i / rate) for i in range(int(seconds * rate))])


def speechlike(seconds=4.0, rate=8000, seed=7):
    rng = random.Random(seed)
    samples = []
    for i in range(int(seconds * rate)):
        t = i / rate
        envelope = max(0.0, math.sin(2 * math.pi * 3.1 * t)) * (0.4 + 0.6 * abs(math.sin(2 * math.pi * 0.7 * t)))
        samples.append(9000 * envelope * (0.6 * math.sin(2 * math.pi * 180 * t) + 0.4 * rng.uniform(-1, 1)))
    return wav(samples)


def fake_pack(asset_ids=None):
    asset_ids = tuple(asset_ids or main.load_prompts(main.VOICE_PROMPTS_PATH))
    content = speechlike()
    assets = {a: VoiceAsset(a, content, hashlib.sha256(content).hexdigest(), 4.0, a) for a in asset_ids}
    return VoicePackStatus(pack_dir=main.settings.project_path('artifacts/voice-pack'), required=list(asset_ids),
                           manifest_present=True, reference_matches=True, assets=assets)


LIVE = dict(mock_mode=False, app_mode='live', call_provider='asterisk', asterisk_ami_secret='test-secret',
            outbound_allowed_destinations=DEST, fish_speech_enabled=True)


@pytest.fixture
def live(monkeypatch):
    for key, value in LIVE.items():
        monkeypatch.setattr(main.settings, key, value)
    store = CallStore(':memory:')
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main.provider, 'store', store)
    return store


# Configuration ------------------------------------------------------------------------
def test_demo_path_in_public_base_url_is_an_error_not_stripped():
    cfg = Settings(_env_file=None, **LIVE, public_base_url='https://host.example.test/demo').resolve()
    assert cfg.public_origin == ''
    assert any('PUBLIC_BASE_URL must be the public origin only' in e and '/demo' in e for e in cfg.errors)


@pytest.mark.parametrize('overrides,fragment', [
    ({'public_base_url': 'http://host.example.test'}, 'must use https'),
    ({'public_base_url': 'https://localhost'}, 'public hostname'),
    ({'public_base_url': 'https://10.0.0.5'}, 'public hostname'),
    ({'public_base_url': 'https://user:pw@host.example.test'}, 'credentials'),
    ({'public_base_url': 'https://[bad'}, 'not a valid URL'),
    ({'app_mode': 'demo'}, 'MOCK_MODE=false requires APP_MODE=live'),
    ({'call_provider': 'mock'}, 'CALL_PROVIDER=asterisk'),
    ({'telephony_interface': 'carrier-pigeon'}, 'TELEPHONY_INTERFACE must be one of'),
    ({'telephony_interface': 'sip'}, 'requires SIP_TRUNK_HOST'),
    ({'outbound_channel_template': 'PJSIP/fixed-number'}, 'OUTBOUND_CHANNEL_TEMPLATE'),
    ({'outbound_caller_id': '12345'}, 'OUTBOUND_CALLER_ID must be an E.164'),
    ({'outbound_allowed_destinations': '9908552414'}, 'E.164'),
    ({'fish_speech_enabled': False}, 'FISH_SPEECH_ENABLED'),
    ({'asterisk_ami_secret': ''}, 'ASTERISK_AMI_SECRET'),
])
def test_live_configuration_contradictions_are_reported(overrides, fragment):
    cfg = Settings(_env_file=None, **{**LIVE, **overrides}).resolve()
    assert any(fragment in e for e in cfg.errors), cfg.errors


def test_clean_live_configuration_resolves_one_effective_value():
    cfg = Settings(_env_file=None, **LIVE, telephony_interface='sip', sip_trunk_host='sip.example.net',
                   sip_trunk_username='u', sip_trunk_password='p', outbound_caller_id='+914000000000',
                   public_base_url='https://Host.Example.test:443/').resolve()
    assert cfg.errors == []
    assert cfg.asterisk_enabled and cfg.call_provider == 'asterisk'
    assert cfg.channel_template == 'PJSIP/{e164}@pstn-trunk'
    assert cfg.public_origin == 'https://host.example.test'
    assert cfg.allowed_destinations == [DEST]


def test_live_mode_starts_without_any_telephony_line_configured():
    cfg = Settings(_env_file=None, **LIVE).resolve()
    assert cfg.errors == [] and cfg.telephony_interface == '' and cfg.channel_template == ''


def test_mock_mode_never_enables_asterisk():
    cfg = Settings(_env_file=None, mock_mode=True, call_provider='asterisk').resolve()
    assert cfg.call_provider == 'mock' and not cfg.asterisk_enabled


def test_clinic_phone_override_does_not_change_approved_answers():
    assert any('CLINIC_PHONE' in w for w in Settings(_env_file=None, clinic_phone='+919908552414').resolve().warnings)
    assert main.CLINIC['phone'] == '+91 96423 40630'


def test_asterisk_reads_the_project_voice_pack_through_wsl_paths():
    assert to_wsl_path(r'E:\Mouth Care\artifacts\voice-pack') == '/mnt/e/Mouth Care/artifacts/voice-pack'
    s = Settings(_env_file=None)
    assert s.asterisk_voice_path().startswith('/mnt/') and s.asterisk_voice_path().endswith('/artifacts/voice-pack')
    assert str(s.local_recording_path()).startswith('\\\\wsl.localhost\\Ubuntu\\var\\spool\\asterisk')


# Phone conversation --------------------------------------------------------------------
def converse(utterances, direction='outbound'):
    state = phone_agent.new_session(direction)
    turns = []
    for text in utterances:
        turn = phone_agent.respond(state, text)
        state = turn.state
        turns.append(turn)
    return turns


def test_appointment_request_is_collected_confirmed_and_never_booked():
    turns = converse(['I would like to book an appointment', 'next Monday', 'around 11 in the morning',
                      'Ravi Kumar', 'tooth sensitivity', 'yes please', 'no that is all'])
    assert [t.prompts[0] for t in turns] == ['appointment_request', 'ask_preferred_time', 'ask_patient_name',
                                            'ask_reason_for_visit', 'confirm_appointment', 'appointment_recorded', 'goodbye']
    assert turns[5].appointment == {'preferred_date': 'next Monday', 'preferred_time': 'around 11 in the morning',
                                    'patient_name': 'Ravi Kumar', 'reason': 'tooth sensitivity'}
    assert turns[-1].hangup is True


@pytest.mark.parametrize('utterance,first_prompt', [
    ('What are your clinic hours?', 'clinic_hours'), ('Where are you located?', 'clinic_address'),
    ('What is your phone number?', 'clinic_phone'), ('What is your email?', 'clinic_email'),
    ('What services do you provide?', 'services'), ('I have severe pain and swelling', 'emergency_care'),
    ('How much does a root canal cost?', 'unsupported_question'), ('Do you accept insurance?', 'unsupported_question'),
    ('Which doctor is available?', 'unsupported_question'), ('Can someone call me back?', 'callback_noted'),
    ('I need a checkup', 'appointment_request'), ('hello', 'generic_help'), ('blue elephant', 'generic_help'),
])
def test_intents_map_to_grounded_packaged_prompts(utterance, first_prompt):
    turn = converse([utterance], direction='inbound')[0]
    assert turn.prompts[0] == first_prompt
    assert set(turn.prompts) <= set(main.load_prompts(main.VOICE_PROMPTS_PATH))


def test_unsupported_questions_create_callback_topics():
    turn = converse(['How much is teeth whitening?'])[0]
    assert turn.callback_topic == 'How much is teeth whitening?' and turn.prompts == ['unsupported_question', 'anything_else']


def test_emergency_interrupts_appointment_collection():
    turns = converse(['book an appointment', 'my gums are bleeding a lot'])
    assert turns[1].prompts == ['emergency_care', 'anything_else'] and turns[1].state['stage'] == 'idle'


def test_silence_reprompts_then_hangs_up():
    turns = converse(['', '', ''])
    assert [t.hangup for t in turns] == [False, False, True]


def test_declined_confirmation_records_nothing():
    turns = converse(['appointment', 'Friday', 'evening', 'Asha', 'cleaning', 'no'])
    assert turns[-1].prompts == ['appointment_declined', 'anything_else'] and turns[-1].appointment is None


def test_turn_limit_ends_the_call():
    state = phone_agent.new_session('inbound')
    for _ in range(3):
        turn = phone_agent.respond(state, 'blue elephant', max_turns=2)
        state = turn.state
    assert turn.hangup is True and turn.intent == 'turn_limit'


def test_every_prompt_the_agent_can_emit_is_in_the_voice_pack_definition():
    prompts = set(main.load_prompts(main.VOICE_PROMPTS_PATH))
    utterances = ['', 'hello', 'blue elephant', 'what are your hours', 'where are you located', 'your phone number',
                  'email', 'what services', 'severe pain', 'how much does it cost', 'call me back', 'book an appointment',
                  'next monday', 'yes', 'no', 'cancel', 'bye', 'thank you']
    emitted = {phone_agent.greeting_prompt('inbound'), phone_agent.greeting_prompt('outbound')}
    frontier = [phone_agent.new_session(direction) for direction in ('inbound', 'outbound')]
    for _ in range(8):  # explore every reachable conversation state for 8 turns
        reached = {}
        for state in frontier:
            for utterance in utterances:
                turn = phone_agent.respond(state, utterance)
                emitted.update(turn.prompts)
                if not turn.hangup:
                    s = turn.state
                    reached.setdefault((s['stage'], s['last_prompt'], s['silence'], s['unclear'], s['direction']), s)
        frontier = list(reached.values())
    assert emitted <= prompts, emitted - prompts
    assert {'appointment_recorded', 'appointment_declined', 'callback_noted', 'unsupported_question', 'emergency_care',
            'goodbye', 'repeat_or_not_understood', 'confirm_appointment'} <= emitted


# Voice pack validation -------------------------------------------------------------------
def build_pack(tmp_path, clips, prompts, *, reference=b'reference-bytes', manifest_reference=None, **entry_overrides):
    pack = tmp_path / 'pack'
    pack.mkdir()
    ref = tmp_path / 'reference.wav'
    ref.write_bytes(reference)
    assets = []
    for asset_id, content in clips.items():
        (pack / f'{asset_id}.wav').write_bytes(content)
        assets.append({'asset_id': asset_id, 'sha256': hashlib.sha256(content).hexdigest(), 'source_text': prompts[asset_id]['text'],
                       'generation_status': GENERATION_STATUS, 'validation_status': VALIDATION_STATUS,
                       'asr': {'transcript': prompts[asset_id]['text']}, **entry_overrides})
    manifest = {'pack_version': '2.0.0', 'reference_audio_sha256': manifest_reference or hashlib.sha256(reference).hexdigest(), 'assets': assets}
    (pack / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return pack, ref


PROMPTS = {'greeting': {'text': 'Thank you for calling. How may I help?'}}


def test_voice_pack_accepts_verified_speech(tmp_path):
    pack, ref = build_pack(tmp_path, {'greeting': speechlike()}, PROMPTS)
    status = load_voice_pack(pack, PROMPTS, ref)
    assert status.valid and status.complete and set(status.assets) == {'greeting'}


@pytest.mark.parametrize('case,expected', [
    ('tone', 'not_speech:greeting'), ('status', 'not_fish_reference_generated:greeting'),
    ('asr', 'not_asr_verified:greeting'), ('text', 'text_changed:greeting'),
    ('reference', 'reference_audio_mismatch'), ('hash', 'hash:greeting'), ('transcript', 'asr_transcript_missing:greeting'),
])
def test_voice_pack_rejects_fake_or_drifted_assets(tmp_path, case, expected):
    clip = tone() if case == 'tone' else speechlike()
    overrides = {'status': {'generation_status': 'preexisting_asset_unverified'},
                 'asr': {'validation_status': 'telephone_wav_valid'},
                 'text': {'source_text': 'Something else entirely.'},
                 'transcript': {'asr': {}}}.get(case, {})
    pack, ref = build_pack(tmp_path, {'greeting': clip}, PROMPTS,
                           manifest_reference='0' * 64 if case == 'reference' else None, **overrides)
    if case == 'hash':
        (pack / 'greeting.wav').write_bytes(speechlike(seed=99))
    status = load_voice_pack(pack, PROMPTS, ref)
    assert not status.valid and expected in status.errors and 'greeting' not in status.assets


def test_voice_pack_rejects_wrong_emergency_wording(tmp_path):
    prompts = {'emergency_care': {'text': 'Swelling affecting breathing or swallowing.', 'required_phrases': ['breathing or swallowing']}}
    pack, ref = build_pack(tmp_path, {'emergency_care': speechlike()}, prompts,
                           asr={'transcript': 'Swelling affecting bleeding or swallowing.'})
    status = load_voice_pack(pack, prompts, ref)
    assert 'asr_required_phrase_missing:breathing or swallowing:emergency_care' in status.errors


def test_sine_tone_placeholders_measure_as_non_speech():
    assert speech_metrics(tone())['energy_modulation'] < 0.05
    assert speech_metrics(speechlike())['energy_modulation'] > 0.3


def test_project_voice_pack_is_the_verified_fish_reference_voice():
    pack = main.voice_pack()
    prompts = main.load_prompts(main.VOICE_PROMPTS_PATH)
    assert pack.errors == [] and pack.valid, pack.errors
    assert set(pack.assets) == set(prompts)
    manifest = json.loads((main.settings.project_path(main.settings.fish_speech_voice_pack_dir) / 'manifest.json').read_text(encoding='utf-8'))
    reference = main.settings.project_path(main.settings.fish_speech_reference_audio)
    assert manifest['reference_audio_sha256'] == hashlib.sha256(reference.read_bytes()).hexdigest()
    for entry in manifest['assets']:
        assert entry['generation_status'] == GENERATION_STATUS and entry['validation_status'] == VALIDATION_STATUS
        with wave.open(str(main.settings.project_path(entry['file_path'])), 'rb') as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)


# Booking fail-closed in live mode --------------------------------------------------------------
@pytest.mark.parametrize('method,path,payload', [
    ('get', '/api/slots', None),
    ('post', '/api/booking/hold', {'slot_id': 'demo-slot-0', 'session_id': 's'}),
    ('post', '/api/booking/confirm', {'hold_token': 'x', 'patient_details': {}, 'consent': True, 'idempotency_key': 'k'}),
    ('post', '/api/booking/reschedule', {'booking_reference': 'x', 'new_slot_id': 'y', 'verification': 'z'}),
    ('post', '/api/booking/cancel', {'booking_reference': 'x', 'reason': 'r', 'verification': 'z'}),
])
def test_live_mode_never_serves_demo_booking(live, method, path, payload):
    client = TestClient(main.app)
    response = client.get(path) if method == 'get' else client.post(path, json=payload)
    assert response.status_code == 503
    assert response.json()['detail']['error'] == 'live_booking_repository_unavailable'


def test_live_web_receptionist_offers_requests_not_demo_slots(live):
    body = TestClient(main.app).post('/api/agent/session', json={'message': 'I want to book an appointment'}).json()
    assert 'slots' not in body and 'not connected' in body['text']


# Admin stays disabled unless securely configured --------------------------------------------
STRONG_SECRET = 's' * 16 + 'T' * 16 + '9' * 8


def _token(payload: str) -> str:
    return 'mcs.' + main.base64.urlsafe_b64encode(payload.encode()).decode() + '.' + main._sign(payload)


@pytest.mark.parametrize('email,password,secret,reason', [
    ('', '', STRONG_SECRET, 'ADMIN_INITIAL_EMAIL'),
    ('admin@example.test', 'correct-horse-battery', 'short-secret', 'JWT_SECRET is weak'),
    ('admin@example.test', 'correct-horse-battery', 'change-me-in-live-mode', 'JWT_SECRET is weak'),
    ('admin@example.test', 'short-pw1', STRONG_SECRET, 'shorter than 12'),
])
def test_live_admin_is_disabled_without_secure_configuration(live, monkeypatch, email, password, secret, reason):
    for key, value in dict(admin_initial_email=email, admin_initial_password=password, jwt_secret=secret).items():
        monkeypatch.setattr(main.settings, key, value)
    client = TestClient(main.app)
    response = client.post('/api/admin/login', json={'email': email or 'x@example.test', 'password': password or 'whatever-pass'})
    assert response.status_code == 503 and reason in response.json()['detail']['reason']
    forged = _token(f'admin|{int(main.time.time()) + 60}|n')
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + forged}).status_code == 403
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer demo-admin-token'}).status_code == 403


def test_live_admin_works_only_with_strong_configuration(live, monkeypatch):
    for key, value in dict(admin_initial_email='admin@example.test', admin_initial_password='correct-horse-battery',
                           jwt_secret=STRONG_SECRET).items():
        monkeypatch.setattr(main.settings, key, value)
    client = TestClient(main.app)
    assert client.post('/api/admin/login', json={'email': 'admin@example.test', 'password': 'wrong-password!'}).status_code == 401
    token = client.post('/api/admin/login', json={'email': 'admin@example.test', 'password': 'correct-horse-battery'}).json()['access_token']
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + token}).status_code == 200
    expired = _token(f'admin|{int(main.time.time()) - 1}|n')
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + expired}).status_code == 403


# Cached readiness for the web client ----------------------------------------------------------
GREEN = {'SOFTWARE_READY_FOR_LIVE_CALL': True, 'LIVE_CALL_ALLOWED': True, 'BLOCKERS': [], 'CHECKED_AT': 'T'}


def test_cached_readiness_is_anded_with_live_checks(monkeypatch):
    monkeypatch.setattr(main, '_preflight_cache', {'started': 1.0, 'at': main.time.monotonic(), 'result': dict(GREEN), 'running': False})
    ok = main._cached_readiness({'voice_pack_valid': True, 'fish_speech_ready': True})
    assert ok['LIVE_CALL_ALLOWED'] is True
    broken = main._cached_readiness({'voice_pack_valid': False, 'fish_speech_ready': True})
    assert broken['LIVE_CALL_ALLOWED'] is False and broken['SOFTWARE_READY_FOR_LIVE_CALL'] is False
    assert 'CHANGED_SINCE_PREFLIGHT: voice_pack_valid' in broken['BLOCKERS']


def test_stale_or_missing_readiness_is_unknown(monkeypatch):
    monkeypatch.setattr(main, '_preflight_cache', {'started': 1.0, 'at': main.time.monotonic() - main._PREFLIGHT_MAX_AGE - 1,
                                                   'result': dict(GREEN), 'running': False})
    assert main._cached_readiness({'x': True}) is None
    monkeypatch.setattr(main, '_preflight_cache', {'started': 0.0, 'at': 0.0, 'result': None, 'running': False})
    assert main._cached_readiness({'x': True}) is None


def test_older_preflight_run_never_overwrites_a_newer_result(monkeypatch):
    monkeypatch.setattr(main, '_preflight_cache', {'started': 0.0, 'at': 0.0, 'result': None, 'running': False})
    results = iter([{'LIVE_CALL_ALLOWED': True, 'n': 'old'}, {'LIVE_CALL_ALLOWED': False, 'n': 'new'}])
    starts = iter([10.0, 11.0, 20.0, 21.0])  # (start, finish) of old run, then of new run
    monkeypatch.setattr(main, '_telephony_preflight', lambda destination, refresh=False: next(results))
    monkeypatch.setattr(main.time, 'monotonic', lambda: next(starts))
    main._run_preflight()
    main._run_preflight()
    assert main._preflight_cache['result']['n'] == 'new'
    monkeypatch.setattr(main, '_telephony_preflight', lambda destination, refresh=False: {'LIVE_CALL_ALLOWED': True, 'n': 'stale'})
    monkeypatch.setattr(main.time, 'monotonic', lambda: 15.0)  # a run that started before the newest one
    main._run_preflight()
    assert main._preflight_cache['result']['n'] == 'new'


def test_failed_preflight_clears_cached_readiness(monkeypatch):
    monkeypatch.setattr(main, '_preflight_cache', {'started': 1.0, 'at': 2.0, 'result': dict(GREEN), 'running': False})

    def crash(destination, refresh=False):
        raise RuntimeError('boom')
    monkeypatch.setattr(main, '_telephony_preflight', crash)
    with pytest.raises(RuntimeError):
        main._run_preflight()
    assert main._preflight_cache['result'] is None


def test_demo_status_has_no_live_readiness():
    body = TestClient(main.app).get('/api/status').json()
    assert body['mode'] == 'demo' and body['readiness'] is None and body['telephony'] == 'mock'
