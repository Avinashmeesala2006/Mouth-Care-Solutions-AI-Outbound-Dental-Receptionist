import io
import json
import math
import asyncio
import re
import wave
from pathlib import Path

import pytest

from backend.app.adapters.fish_speech import AudioArtifact, FishSpeechAdapter, FishSpeechError
from fastapi.testclient import TestClient


def wav_bytes(sample_rate=16000, seconds=0.2):
    frames = int(sample_rate * seconds)
    samples = bytearray()
    for i in range(frames):
        value = int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        samples.extend(value.to_bytes(2, 'little', signed=True))
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(samples))
    return output.getvalue()


class FakeResponse:
    content = wav_bytes()

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.payload = None
        self.headers = None
        self.data = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, endpoint, json=None, data=None, headers=None):
        self.payload = json
        self.data = data
        self.headers = headers
        return FakeResponse()


def test_fish_speech_uses_msgpack_reference_contract(monkeypatch):
    seen = {}

    class RecordingClient(FakeClient):
        async def post(self, endpoint, json=None, data=None, headers=None):
            seen['endpoint'] = endpoint
            seen['json'] = json
            seen['data'] = data
            seen['headers'] = headers
            return FakeResponse()

    monkeypatch.setattr('backend.app.adapters.fish_speech.httpx.AsyncClient', RecordingClient)
    adapter = FishSpeechAdapter(
        'http://127.0.0.1:8080',
        max_new_tokens=400,
        reference_audio='fish-references/mouth-care-receptionist-reference-20260919.wav',
        reference_text='Reference speaker transcript.',
    )
    asyncio.run(adapter.synthesize('Hello from the receptionist.'))

    assert seen['endpoint'] == 'http://127.0.0.1:8080/v1/tts'
    assert seen['headers']['content-type'] == 'application/msgpack'
    assert seen['json'] is None
    assert seen['data'] is not None
    assert b'acts as a valid msgpack payload' not in seen['data']
    assert seen['data']


def test_fish_speech_generates_telephone_wav(monkeypatch):
    monkeypatch.setattr('backend.app.adapters.fish_speech.httpx.AsyncClient', FakeClient)
    adapter = FishSpeechAdapter('http://127.0.0.1:8080')
    artifact = asyncio.run(adapter.synthesize('Hello from the receptionist.'))
    assert artifact.source == 'fish-speech'
    assert artifact.sample_rate == 8000
    assert artifact.channels == 1
    assert artifact.duration_seconds > 0
    with wave.open(io.BytesIO(artifact.content), 'rb') as wav_file:
        assert wav_file.getframerate() == 8000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2


def test_fish_speech_rejects_empty_text():
    adapter = FishSpeechAdapter('http://127.0.0.1:8080')
    with pytest.raises(ValueError, match='tts_text_required'):
        asyncio.run(adapter.synthesize('  '))


def test_fish_speech_payload_includes_reference_voice(tmp_path):
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'reference-audio')
    adapter = FishSpeechAdapter(
        'http://127.0.0.1:8080',
        reference_audio=str(reference),
        reference_text='Reference speaker transcript.',
    )
    payload = adapter._payload('Hello from the receptionist.')
    assert payload['references'][0]['text'] == 'Reference speaker transcript.'
    assert payload['references'][0]['audio']


def test_missing_reference_voice_fails_cleanly():
    adapter = FishSpeechAdapter(
        'http://127.0.0.1:8080',
        reference_audio='/tmp/does-not-exist-mouth-care-reference.wav',
    )
    with pytest.raises(FishSpeechError, match='fish_speech_reference_unavailable'):
        asyncio.run(adapter.synthesize('Hello from the receptionist.'))


def test_voice_pack_manifest_has_complete_required_assets():
    from backend.app.services.voice_pack import GENERATION_STATUS, VALIDATION_STATUS, speech_metrics

    root = Path(__file__).resolve().parents[2]
    pack_dir = root / 'artifacts' / 'voice-pack'
    manifest_path = pack_dir / 'manifest.json'

    assert manifest_path.exists(), 'Missing generated voice-pack manifest'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    required = set(json.loads((root / 'knowledge' / 'clinic' / 'voice_prompts.json').read_text(encoding='utf-8'))['prompts'])
    asset_ids = {entry['asset_id'] for entry in manifest['assets']}
    assert asset_ids == required

    for entry in manifest['assets']:
        wav_path = pack_dir / f"{entry['asset_id']}.wav"
        assert wav_path.exists(), f"Missing required voice asset: {entry['asset_id']}"
        metrics = speech_metrics(wav_path.read_bytes())
        assert (metrics['sample_rate'], metrics['channels'], metrics['sample_width']) == (8000, 1, 2)
        assert metrics['energy_modulation'] > 0.3, f"{entry['asset_id']} is not speech"
        assert entry['generation_status'] == GENERATION_STATUS
        assert entry['validation_status'] == VALIDATION_STATUS


@pytest.mark.parametrize(
    ('speech', 'prompt'),
    [
        ('What are your clinic hours?', 'clinic_hours'),
        ('Where are you located?', 'clinic_address'),
        ('What is your phone number?', 'clinic_phone'),
        ('What is your email?', 'clinic_email'),
        ('What services do you provide?', 'services'),
        ('I need emergency dental care.', 'emergency_care'),
        ('I want to book an appointment.', 'appointment_request'),
        ('', 'repeat_or_not_understood'),
        ('Tell me something not in the clinic information.', 'generic_help'),
    ],
)
def test_supported_faq_intent_selection(speech, prompt):
    from backend.app.services import phone_agent

    turn = phone_agent.respond(phone_agent.new_session('inbound'), speech)
    assert turn.prompts[0] == prompt


def test_ready_distinguishes_fish_configuration_and_reachability(monkeypatch):
    from backend.app import main

    monkeypatch.setattr(main.settings, 'mock_mode', True)
    monkeypatch.setattr(main.settings, 'fish_speech_enabled', False)
    demo = TestClient(main.app).get('/ready')
    assert demo.status_code == 200
    assert demo.json()['fish_speech'] == {'configured': False, 'reachable': False, 'required_during_calls': False}
    assert demo.json()['booking_repository'] == 'in-memory-demo'
    assert demo.json()['booking_repository_ready'] is True

    monkeypatch.setattr(main.settings, 'mock_mode', False)
    monkeypatch.setattr(main.settings, 'app_mode', 'live')
    monkeypatch.setattr(main.settings, 'fish_speech_enabled', True)
    monkeypatch.setattr(main.settings, 'fish_speech_base_url', 'http://fish-speech:8080')
    monkeypatch.setattr(main.httpx, 'get', lambda *args, **kwargs: (_ for _ in ()).throw(main.httpx.ConnectError('offline')))
    unavailable = TestClient(main.app).get('/ready')
    assert unavailable.status_code == 503
    body = unavailable.json()
    assert body['status'] == 'not_ready' and body['mode'] == 'live'
    assert body['fish_speech']['reachable'] is False
    assert body['booking_repository'] == 'unavailable-live'
    assert body['booking_repository_ready'] is False
    assert any('CALL_PROVIDER=asterisk' in e for e in body['config_errors'])
