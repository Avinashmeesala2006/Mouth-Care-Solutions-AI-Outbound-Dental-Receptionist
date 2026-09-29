"""Receptionist core: truthful configuration, grounded phone conversation, verified voice
pack, fail-closed booking and admin lock-down."""
import hashlib
import io
import json
import math
import random
import wave

import pytest
from conftest import FROM_NUMBER, PUBLIC
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.api import security
from backend.app.core.config import Settings, normalize_e164
from backend.app.services import phone_agent
from backend.app.services.voice_pack import GENERATION_STATUS, VALIDATION_STATUS, load_prompts, load_voice_pack, speech_metrics


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


def production(**overrides):
    values = dict(app_mode='production', twilio_enabled=True, twilio_account_sid='AC' + '1' * 32,
                  twilio_auth_token='t' * 32, twilio_from_number=FROM_NUMBER, public_base_url=PUBLIC,
                  database_url='postgresql://app:pw@db.internal:5432/mouthcare',
                  fish_speech_base_url='http://127.0.0.1:8080', jwt_secret='j' * 48,
                  fish_speech_reference_audio='fish-references/mouth-care-receptionist-reference-20260919.wav')
    values.update(overrides)
    return Settings(_env_file=None, **values)


# Configuration ----------------------------------------------------------------------------------
def test_clean_production_configuration_has_no_errors():
    cfg = production().resolve()
    assert cfg.errors == [], cfg.errors
    assert cfg.production_like and cfg.twilio_enabled and cfg.call_provider == 'twilio'
    assert cfg.outbound_url == f'{PUBLIC}/api/telephony/twilio/outbound'
    assert cfg.quota_seconds == 180000 and cfg.from_number == FROM_NUMBER


def test_legacy_provider_keeps_its_explicit_asterisk_requirement():
    cfg = Settings(_env_file=None, app_mode='production', call_provider='asterisk').resolve()
    assert any('requires ASTERISK_ENABLED=true' in error for error in cfg.errors)


@pytest.mark.parametrize('overrides,fragment', [
    ({'twilio_enabled': False}, 'requires TWILIO_ENABLED=true'),
    ({'twilio_account_sid': ''}, 'TWILIO_ACCOUNT_SID'),
    ({'twilio_auth_token': ''}, 'TWILIO_AUTH_TOKEN'),
    ({'twilio_from_number': ''}, 'TWILIO_FROM_NUMBER'),
    ({'twilio_from_number': '12'}, 'TWILIO_FROM_NUMBER must be a valid E.164'),
    ({'public_base_url': 'http://voice.example.com'}, 'must use https'),
    ({'public_base_url': 'https://localhost'}, 'public hostname'),
    ({'public_base_url': 'https://voice.example.com/demo'}, 'public origin only'),
    ({'twilio_validate_signature': False}, 'TWILIO signature validation'),
    ({'database_url': ''}, 'requires DATABASE_URL'),
    ({'database_url': 'sqlite:///data.db'}, 'PostgreSQL URL'),
    ({'fish_speech_enabled': False}, 'FISH_SPEECH_ENABLED=false'),
    ({'jwt_secret': 'change-me-in-live-mode'}, 'JWT_SECRET is weak'),
    ({'service_quota_hours': 50, 'service_quota_seconds': 1000}, 'disagree'),
    ({'max_concurrent_app_calls': 0}, 'MAX_CONCURRENT_APP_CALLS'),
    ({'outbound_allowed_destinations': '9000000000'}, 'E.164'),
    ({'app_mode': 'testing'}, 'APP_MODE must be one of'),
])
def test_production_fails_closed(overrides, fragment):
    cfg = production(**overrides).resolve()
    assert any(fragment in e for e in cfg.errors), cfg.errors


def test_production_requires_twilio_credentials():
    errors = Settings(_env_file=None, app_mode='production').resolve().errors
    assert any('TWILIO_ENABLED=true' in e for e in errors)


def test_development_runs_without_twilio_or_postgres_but_says_so():
    cfg = Settings(_env_file=None).resolve()
    assert cfg.errors == [] and not cfg.twilio_enabled and cfg.call_provider == 'disabled'
    assert any('in memory' in w for w in cfg.warnings)


def test_legacy_modes_are_mapped_with_a_warning():
    assert Settings(_env_file=None, app_mode='demo').mode == 'development'
    cfg = production(app_mode='live').resolve()
    assert cfg.app_mode == 'production' and any('legacy' in w for w in cfg.warnings)


def test_quota_configuration_and_aliases():
    assert Settings(_env_file=None).quota_seconds() == 180000
    assert Settings(_env_file=None, service_quota_hours=2).quota_seconds() == 7200
    assert Settings(_env_file=None, service_quota_seconds=900).quota_seconds() == 900
    assert Settings(_env_file=None, FISH_SPEECH_GIT_REF='v1.5.1').fish_speech_version == 'v1.5.1'


@pytest.mark.parametrize('raw,expected', [('+12025550143', '+12025550143'), ('12025550143', '+12025550143'),
                                          ('9000000000', '+919000000000'), ('09000000000', '+919000000000'),
                                          ('919000000000', '+919000000000'), ('0012025550143', '+12025550143'),
                                          ('+91 90000-00000', '+919000000000'), ('abc', None), ('123', None),
                                          ('+٩١٩٠٠٠٠٠٠٠٠٠', None), ('9000000000; DROP', None)])
def test_phone_numbers_are_normalized_to_e164(raw, expected):
    assert normalize_e164(raw, '91') == expected


def test_clinic_phone_override_does_not_change_approved_answers():
    assert any('CLINIC_PHONE' in w for w in Settings(_env_file=None, clinic_phone='+919000000001').resolve().warnings)
    assert main.CLINIC['phone'] == '+91 96423 40630'


# Phone conversation -----------------------------------------------------------------------------------
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


def test_details_can_be_corrected_before_confirmation():
    turns = converse(['book an appointment', 'Friday', 'morning', 'Asha', 'cleaning', 'actually change the date',
                      'Saturday', 'yes'])
    assert turns[5].intent == 'appointment_correct_preferred_date' and turns[5].prompts == ['ask_preferred_date']
    assert turns[6].prompts == ['confirm_appointment']                     # straight back to confirmation
    assert turns[7].appointment == {'preferred_date': 'Saturday', 'preferred_time': 'morning', 'patient_name': 'Asha',
                                    'reason': 'cleaning'}


@pytest.mark.parametrize('reply', ["that's not correct", 'no', 'that is wrong', 'incorrect'])
def test_negative_confirmation_never_records_the_request(reply):
    turns = converse(['appointment', 'Friday', 'evening', 'Asha', 'cleaning', reply])
    assert turns[-1].appointment is None and turns[-1].prompts[0] == 'appointment_declined'


@pytest.mark.parametrize('utterance', ['please do not call me again', "don't call me", 'stop calling me',
                                       'remove my number', 'unsubscribe', "don't contact me", 'Do not call.'])
def test_opt_out_requests_end_the_call_in_every_stage(utterance):
    for prefix in ([], ['book an appointment'], ['book an appointment', 'Monday', 'noon', 'Ravi', 'pain']):
        turn = converse(prefix + [utterance])[-1]
        assert turn.opt_out and turn.hangup and turn.prompts == ['opt_out_confirmed'], (prefix, utterance)


@pytest.mark.parametrize('utterance', ['call me back please', 'I phone often from work', 'I do not have pain',
                                       'can you call me tomorrow'])
def test_ordinary_sentences_are_not_opt_outs(utterance):
    assert not converse([utterance])[-1].opt_out


def test_cancellations_become_callback_requests_not_new_bookings():
    turn = converse(['I need to cancel my appointment'])[0]
    assert turn.intent == 'cancellation_callback' and turn.prompts == ['callback_noted', 'anything_else']
    assert turn.callback_topic.startswith('Appointment cancellation request')


@pytest.mark.parametrize('utterance,first_prompt', [
    ('What are your clinic hours?', 'clinic_hours'), ('Where are you located?', 'clinic_address'),
    ('What is your phone number?', 'clinic_phone'), ('What is your email?', 'clinic_email'),
    ('What is your email address?', 'clinic_email'), ('What is your address?', 'clinic_address'),
    ('What services do you provide?', 'services'), ('I have severe pain and swelling', 'emergency_care'),
    ('How much does a root canal cost?', 'unsupported_question'), ('Do you accept insurance?', 'unsupported_question'),
    ('Which doctor is available?', 'unsupported_question'), ('Can someone call me back?', 'callback_noted'),
    ('I need a checkup', 'appointment_request'), ('hello', 'generic_help'), ('blue elephant', 'generic_help'),
])
def test_intents_map_to_grounded_packaged_prompts(utterance, first_prompt):
    turn = converse([utterance], direction='inbound')[0]
    assert turn.prompts[0] == first_prompt
    assert set(turn.prompts) <= set(load_prompts(main.runtime.settings.project_path('knowledge/clinic/voice_prompts.json')))


def test_emergency_interrupts_appointment_collection():
    turns = converse(['book an appointment', 'my gums are bleeding a lot'])
    assert turns[1].prompts == ['emergency_care', 'anything_else'] and turns[1].state['stage'] == 'idle'


def test_silence_reprompts_then_hangs_up():
    assert [t.hangup for t in converse(['', '', ''])] == [False, False, True]


def test_turn_limit_ends_the_call():
    state = phone_agent.new_session('inbound')
    for _ in range(3):
        turn = phone_agent.respond(state, 'blue elephant', max_turns=2)
        state = turn.state
    assert turn.hangup is True and turn.intent == 'turn_limit'


def test_every_prompt_the_agent_can_emit_is_in_the_voice_pack_definition():
    prompts = set(load_prompts(main.runtime.settings.project_path('knowledge/clinic/voice_prompts.json')))
    utterances = ['', 'hello', 'blue elephant', 'what are your hours', 'where are you located', 'your phone number',
                  'email', 'what services', 'severe pain', 'how much does it cost', 'call me back', 'book an appointment',
                  'next monday', 'yes', 'no', 'cancel', 'bye', 'thank you', 'change the time', 'cancel my appointment',
                  "don't call me again"]
    emitted = {phone_agent.greeting_prompt('inbound'), phone_agent.greeting_prompt('outbound')}
    frontier = [phone_agent.new_session(direction) for direction in ('inbound', 'outbound')]
    for _ in range(8):
        reached = {}
        for state in frontier:
            for utterance in utterances:
                turn = phone_agent.respond(state, utterance)
                emitted.update(turn.prompts)
                if not turn.hangup:
                    s = turn.state
                    reached.setdefault((s['stage'], s['last_prompt'], s['silence'], s['unclear'], s['direction'],
                                        s.get('correcting')), s)
        frontier = list(reached.values())
    assert emitted <= prompts, emitted - prompts
    assert {'appointment_recorded', 'appointment_declined', 'callback_noted', 'unsupported_question', 'emergency_care',
            'goodbye', 'repeat_or_not_understood', 'confirm_appointment', 'opt_out_confirmed'} <= emitted


# Voice pack validation --------------------------------------------------------------------------------------
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


def test_sine_tone_placeholders_measure_as_non_speech():
    assert speech_metrics(tone())['energy_modulation'] < 0.05
    assert speech_metrics(speechlike())['energy_modulation'] > 0.3


def test_project_voice_pack_is_the_verified_fish_reference_voice():
    pack = main.runtime.voice_pack()
    prompts = main.runtime.prompts
    assert pack.errors == [] and pack.valid, pack.errors
    assert set(pack.assets) == set(prompts) and {'opt_out_confirmed', 'technical_issue', 'lines_busy'} <= set(pack.assets)
    settings = main.runtime.settings
    manifest = json.loads((settings.project_path(settings.fish_speech_voice_pack_dir) / 'manifest.json').read_text(encoding='utf-8'))
    reference = settings.project_path(settings.fish_speech_reference_audio)
    assert manifest['reference_audio_sha256'] == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert manifest['fish_speech_model'] == 'fish-speech-1.5' and manifest['fish_speech_version'] == 'v1.5.1'
    for entry in manifest['assets']:
        assert entry['generation_status'] == GENERATION_STATUS and entry['validation_status'] == VALIDATION_STATUS
        with wave.open(str(settings.project_path(entry['file_path'])), 'rb') as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)


# Fail-closed booking and admin -------------------------------------------------------------------------------
@pytest.mark.parametrize('method,path,payload', [
    ('get', '/api/slots', None),
    ('post', '/api/booking/hold', {'slot_id': 'demo-slot-0', 'session_id': 's'}),
    ('post', '/api/booking/confirm', {'hold_token': 'x', 'patient_details': {}, 'consent': True, 'idempotency_key': 'k'}),
    ('post', '/api/booking/reschedule', {'booking_reference': 'x', 'new_slot_id': 'y', 'verification': 'z'}),
    ('post', '/api/booking/cancel', {'booking_reference': 'x', 'reason': 'r', 'verification': 'z'}),
])
def test_production_never_serves_demo_booking(monkeypatch, method, path, payload):
    monkeypatch.setattr(main.settings, 'app_mode', 'production')
    client = TestClient(main.app)
    response = client.get(path) if method == 'get' else client.post(path, json=payload)
    assert response.status_code == 503 and response.json()['detail']['error'] == 'live_booking_repository_unavailable'


def test_production_web_receptionist_offers_requests_not_demo_slots(monkeypatch):
    monkeypatch.setattr(main.settings, 'app_mode', 'production')
    body = TestClient(main.app).post('/api/agent/session', json={'message': 'I want to book an appointment'}).json()
    assert 'slots' not in body and 'not connected' in body['text']


STRONG_SECRET = 's' * 16 + 'T' * 16 + '9' * 8


def _token(payload: str) -> str:
    return 'mcs.' + security.base64.urlsafe_b64encode(payload.encode()).decode() + '.' + security._sign(payload)


@pytest.mark.parametrize('email,password,secret,reason', [
    ('', '', STRONG_SECRET, 'ADMIN_INITIAL_EMAIL'),
    ('admin@example.test', 'correct-horse-battery', 'short-secret', 'JWT_SECRET is weak'),
    ('admin@example.test', 'correct-horse-battery', 'change-me-in-live-mode', 'JWT_SECRET is weak'),
    ('admin@example.test', 'short-pw1', STRONG_SECRET, 'shorter than 12'),
])
def test_admin_is_disabled_outside_development_without_secure_configuration(monkeypatch, email, password, secret, reason):
    for key, value in dict(app_mode='production', admin_initial_email=email, admin_initial_password=password,
                           jwt_secret=secret).items():
        monkeypatch.setattr(main.settings, key, value)
    client = TestClient(main.app)
    response = client.post('/api/admin/login', json={'email': email or 'x@example.test', 'password': password or 'whatever-pass'})
    assert response.status_code == 503 and reason in response.json()['detail']['reason']
    forged = _token(f'admin|{int(security.time.time()) + 60}|n')
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + forged}).status_code == 403
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer demo-admin-token'}).status_code == 403


def test_admin_works_only_with_strong_configuration(monkeypatch):
    for key, value in dict(app_mode='production', admin_initial_email='admin@example.test',
                           admin_initial_password='correct-horse-battery', jwt_secret=STRONG_SECRET).items():
        monkeypatch.setattr(main.settings, key, value)
    client = TestClient(main.app)
    assert client.post('/api/admin/login', json={'email': 'admin@example.test', 'password': 'wrong-password!'}).status_code == 401
    token = client.post('/api/admin/login', json={'email': 'admin@example.test', 'password': 'correct-horse-battery'}).json()['access_token']
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + token}).status_code == 200
    expired = _token(f'admin|{int(security.time.time()) - 1}|n')
    assert client.get('/api/admin/config', headers={'Authorization': 'Bearer ' + expired}).status_code == 403


def test_demo_status_has_no_live_readiness():
    body = TestClient(main.app).get('/api/status').json()
    assert body['mode'] == 'demo' and body['readiness'] is None and body['telephony'] == 'disabled'
