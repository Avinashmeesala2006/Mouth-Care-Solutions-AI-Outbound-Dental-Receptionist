import importlib.util
import math
import wave
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'generate_voice_pack_http.py'


def load_generator():
    spec = importlib.util.spec_from_file_location('generate_voice_pack_http', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_uses_reference_conditioned_fish_http_contract():
    text = SCRIPT.read_text(encoding='utf-8')
    gen = load_generator()
    assert '"references": [{"audio": reference, "text": REFERENCE_TEXT}]' in text
    assert '/v1/tts' in text and '"use_memory_cache": "on"' in text
    assert gen.GENERATION_PARAMS['max_new_tokens'] >= 512, 'short token caps truncate speech'
    assert gen.GENERATION_STATUS == 'fish_http_reference_conditioned' and gen.VALIDATION_STATUS == 'asr_verified'


def test_generator_asr_checks_detect_wrong_or_missing_words():
    gen = load_generator()
    prompt = {'text': 'Our clinic is open Monday to Saturday.', 'keywords': ['monday', 'saturday']}
    assert gen.asr_report('Our clinic is open Monday to Saturday.', prompt, 'clinic_hours')['errors'] == []
    assert gen.asr_report('Our clinic is closed.', prompt, 'clinic_hours')['errors']
    phone = {'text': 'plus nine one, nine six four two three, four zero six three zero', 'keywords': []}
    assert gen.asr_report('+91 96423 40630', phone, 'clinic_phone')['errors'] == []
    assert 'phone_digits_mismatch' in gen.asr_report('+91 99085 52414', phone, 'clinic_phone')['errors']
    callback = {'text': 'I will ask the clinic team to call you back.', 'keywords': ['clinic']}
    assert 'keywords_missing:clinic' in gen.asr_report('I will ask the plane team to call you back.', callback, 'callback_noted')['errors']


def test_generation_key_changes_when_validation_rules_change():
    gen = load_generator()
    assert gen.generation_key('callback', 'reference') != gen.generation_key('callback', 'reference', 1)


def test_generator_rejects_constant_tone(tmp_path):
    gen = load_generator()
    path = tmp_path / 'tone.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b''.join(int(12000 * math.sin(2 * math.pi * 480 * i / 8000)).to_bytes(2, 'little', signed=True) for i in range(8000 * 5)))
    errors = gen.acoustic_report(path, 'Thank you for calling Mouth Care Solutions.')['errors']
    assert 'no_speech_modulation' in errors


def test_publish_preserves_current_pack_when_staged_validation_fails(tmp_path, monkeypatch):
    gen = load_generator()
    prompts = {'greeting': {'text': 'Hello there.', 'keywords': []}}
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'reference')
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / 'greeting.wav').write_bytes(b'new-audio')
    pack = tmp_path / 'voice-pack'
    pack.mkdir()
    marker = pack / 'old-pack-marker'
    marker.write_text('old-pack-stays-published', encoding='utf-8')
    entry = {
        'raw_sha256': 'raw', 'acoustic': {'frames': 8000, 'duration_seconds': 1.0},
        'asr': {'transcript': 'Hello there.'}, 'seed': 1, 'attempt': 1,
        'generation_seconds': 1.0, 'generated_at': '2026-09-26T00:00:00+00:00',
    }
    state = {'assets': {'greeting': entry}}

    class InvalidPack:
        valid = False
        errors = ['hash:greeting']

    monkeypatch.setattr(gen, 'load_voice_pack', lambda *args: InvalidPack())

    with pytest.raises(RuntimeError, match='staged_voice_pack_invalid'):
        gen.publish(staging, pack, state, prompts, reference, {'version': 'test', 'commit': 'test', 'local_modifications': []}, 'tiny.en')

    assert marker.read_text(encoding='utf-8') == 'old-pack-stays-published'
    assert not list(tmp_path.glob('voice-pack.publish-*'))
