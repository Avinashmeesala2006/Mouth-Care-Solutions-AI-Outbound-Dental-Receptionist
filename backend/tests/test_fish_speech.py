"""Fish Speech 1.5.1 client and the Fish Speech TTS service (voice pack, phrase cache, live)."""
import asyncio
import io
import os
import wave

import httpx
import numpy as np
import ormsgpack
import pytest

from backend.app.core.config import PROJECT_ROOT, settings
from backend.app.voice.audio_adapter import AudioFormat, decode_wav, l16
from backend.app.voice.fish_speech import FishParams, FishSpeechClient, FishSpeechError
from backend.app.voice.tts import FishSpeechTTS, PhraseCache, TtsStats, TtsUnavailable, Utterance, split_sentences

REFERENCE = PROJECT_ROOT / 'fish-references' / 'mouth-care-receptionist-reference-20260919.wav'
REFERENCE_TEXT = 'Tomorrow is holiday because of Sunday.'


def fish_wav(seconds=0.5, rate=44100, channels=1, freq=220.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    pcm = np.round(0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype('<i2')
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def client_for(handler, **kwargs) -> FishSpeechClient:
    return FishSpeechClient(base_url='http://127.0.0.1:8080', reference_audio=REFERENCE, reference_text=REFERENCE_TEXT,
                            params=FishParams(seed=7), transport=httpx.MockTransport(handler), **kwargs)


def test_tts_request_follows_the_v1_5_1_msgpack_contract():
    seen = {}

    def handler(request):
        seen['path'], seen['type'] = request.url.path, request.headers['content-type']
        seen['body'] = ormsgpack.unpackb(request.content)
        return httpx.Response(200, content=fish_wav(), headers={'content-type': 'audio/wav'})

    result = asyncio.run(client_for(handler).synthesize('Hello, thank you for calling Mouth Care Solutions.'))
    body = seen['body']
    assert seen['path'] == '/v1/tts' and seen['type'] == 'application/msgpack'
    assert body['format'] == 'wav' and body['streaming'] is False and body['use_memory_cache'] == 'on'
    assert body['references'][0]['audio'] == REFERENCE.read_bytes() and body['references'][0]['text'] == REFERENCE_TEXT
    assert body['seed'] == 7 and 100 <= body['chunk_length'] <= 300 and body['max_new_tokens'] == 1024
    assert result.fmt == AudioFormat(44100, 1, 2) and abs(result.audio_seconds - 0.5) < 0.01
    assert result.real_time_factor > 0


def test_reference_id_replaces_uploaded_reference_audio():
    seen = {}

    def handler(request):
        seen['body'] = ormsgpack.unpackb(request.content)
        return httpx.Response(200, content=fish_wav())

    client = FishSpeechClient(base_url='http://fish:8080', reference_id='mouth-care', transport=httpx.MockTransport(handler))
    asyncio.run(client.synthesize('Hello.'))
    assert seen['body']['reference_id'] == 'mouth-care' and 'references' not in seen['body']


@pytest.mark.parametrize('handler,code', [
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError('refused', request=r)), 'connection_refused'),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('slow', request=r)), 'timeout'),
    (lambda r: httpx.Response(500, text='No audio generated, please check the input text.'), 'inference_error'),
    (lambda r: httpx.Response(400, text='Text is too long, max length is 100'), 'http_error'),
    (lambda r: httpx.Response(401), 'unauthorized'),
    (lambda r: httpx.Response(200, content=b'ID3\x04not a wav at all'), 'malformed_output'),
    (lambda r: httpx.Response(200, content=fish_wav(channels=2)), 'malformed_output'),
])
def test_fish_failures_are_classified(handler, code):
    with pytest.raises(FishSpeechError) as info:
        asyncio.run(client_for(handler).synthesize('Hello there.'))
    assert info.value.code == code


def test_missing_reference_and_invalid_text_fail_before_any_request():
    calls = []
    client = FishSpeechClient(base_url='http://127.0.0.1:8080', reference_audio=PROJECT_ROOT / 'missing.wav',
                              reference_text='x', transport=httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(FishSpeechError) as info:
        asyncio.run(client.synthesize('Hello.'))
    assert info.value.code == 'reference_unavailable'
    with pytest.raises(FishSpeechError):
        asyncio.run(client_for(lambda r: None).synthesize('   '))
    with pytest.raises(FishSpeechError):
        asyncio.run(client_for(lambda r: None).synthesize('x' * 2001))
    assert calls == []


def test_actual_sample_rate_comes_from_the_wav_header(caplog):
    client = client_for(lambda r: httpx.Response(200, content=fish_wav(rate=24000)), expected_sample_rate=44100)
    result = asyncio.run(client.synthesize('Hello.'))
    assert result.fmt.sample_rate == 24000 and client.last_output_format.sample_rate == 24000
    assert 'fish_speech_sample_rate_differs' in caplog.text


def test_streaming_strips_the_header_and_yields_whole_samples():
    audio = fish_wav(0.3)
    header, pcm = audio[:44], audio[44:]
    streamed_header = bytearray(header)
    streamed_header[40:44] = b'\x00\x00\x00\x00'                     # v1.5.1 streaming header: data size 0

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield bytes(streamed_header[:30])
            yield bytes(streamed_header[30:]) + pcm[:1001]              # odd split inside a sample
            yield pcm[1001:]

    client = client_for(lambda r: httpx.Response(200, stream=Chunks()))

    async def collect():
        return [chunk async for chunk in client.stream('Hello.')]

    chunks = asyncio.run(collect())
    assert all(fmt.sample_rate == 44100 and len(data) % 2 == 0 for fmt, data in chunks)
    assert b''.join(data for _, data in chunks) == pcm


class RawPcmStream(httpx.AsyncByteStream):
    """Fish Speech 1.5.1 streaming body: raw int16 PCM segments without a WAV header."""

    def __init__(self, pcm: bytes):
        self.pcm = pcm

    async def __aiter__(self):
        yield self.pcm[:3001]
        yield self.pcm[3001:]


def test_headerless_v1_5_1_stream_uses_the_known_decoder_rate():
    pcm = fish_wav(0.3)[44:]
    client = client_for(lambda r: httpx.Response(200, stream=RawPcmStream(pcm), headers={'content-type': 'audio/wav'}),
                        expected_sample_rate=44100)

    async def collect():
        return [chunk async for chunk in client.stream('Hello.')]

    chunks = asyncio.run(collect())
    assert {fmt for fmt, _ in chunks} == {AudioFormat(44100, 1, 2)}
    assert b''.join(data for _, data in chunks) == pcm


def test_headerless_stream_without_a_known_rate_is_refused():
    client = client_for(lambda r: httpx.Response(200, stream=RawPcmStream(b'\x01\x00' * 4000)))

    async def collect():
        return [chunk async for chunk in client.stream('Hello.')]

    with pytest.raises(FishSpeechError) as info:
        asyncio.run(collect())
    assert info.value.code == 'malformed_output' and 'unknown sample rate' in str(info.value)


def test_health_distinguishes_configured_reachable_and_loaded():
    ok = asyncio.run(client_for(lambda r: httpx.Response(200, json={'status': 'ok'})).health())
    assert ok.configured and ok.reachable and ok.model_loaded and ok.ready and ok.private_endpoint
    down = asyncio.run(client_for(lambda r: (_ for _ in ()).throw(httpx.ConnectError('x', request=r))).health())
    assert down.configured and not down.reachable and not down.ready and down.error == 'connection_refused'
    busy = asyncio.run(client_for(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('x', request=r))).health())
    assert busy.reachable and not busy.model_loaded and 'busy' in busy.error
    disabled = asyncio.run(FishSpeechClient(base_url='http://127.0.0.1:8080', enabled=False).health())
    assert not disabled.enabled and not disabled.ready and disabled.error == 'disabled'


# TTS service --------------------------------------------------------------------------------------------
@pytest.fixture
def pack():
    from backend.app.main import runtime
    return runtime.voice_pack


def make_tts(tmp_path, handler, pack, *, live=False, streaming=False, budget=4.0):
    client = client_for(handler)
    return FishSpeechTTS(client=client, voice_pack=pack, cache=PhraseCache(tmp_path / 'cache'), target=l16(16000),
                         live_synthesis=live, streaming=streaming, first_audio_budget_seconds=budget)


async def collect(tts, utterance):
    stats = TtsStats()
    chunks = [c async for c in tts.audio(utterance, stats)]
    return b''.join(chunks), stats


def test_voice_pack_assets_are_served_without_synthesis(tmp_path, pack):
    requests = []
    tts = make_tts(tmp_path, lambda r: requests.append(r), pack)
    pcm, stats = asyncio.run(collect(tts, Utterance('Thank you for calling', 'greeting')))
    fmt, pack_pcm = decode_wav(pack().assets['greeting'].content)
    assert fmt == AudioFormat(8000, 1, 2) and abs(len(pcm) - 2 * len(pack_pcm)) <= 8   # 8 kHz asset -> 16 kHz stream
    assert stats.source == 'voice_pack' and requests == []
    assert tts.warm() == len(pack().assets) and tts.fallback_pcm()


def test_unknown_text_is_refused_when_live_synthesis_is_disabled(tmp_path, pack):
    tts = make_tts(tmp_path, lambda r: httpx.Response(200, content=fish_wav()), pack)
    with pytest.raises(TtsUnavailable, match='live_synthesis_disabled'):
        asyncio.run(collect(tts, Utterance('Your appointment is on Tuesday.')))
    assert not tts.can_speak(Utterance('Your appointment is on Tuesday.'))


def test_live_synthesis_caches_by_voice_signature(tmp_path, pack):
    requests = []

    def handler(request):
        requests.append(ormsgpack.unpackb(request.content)['text'])
        return httpx.Response(200, content=fish_wav(0.4))

    tts = make_tts(tmp_path, handler, pack, live=True)
    utterance = Utterance('One moment please. Let me check that for you.')
    pcm, stats = asyncio.run(collect(tts, utterance))
    assert requests == ['One moment please.', 'Let me check that for you.'] and stats.source == 'fish_live'
    assert abs(len(pcm) / 2 / 16000 - 0.8) < 0.01
    again, stats = asyncio.run(collect(tts, utterance))
    assert again == pcm and stats.source == 'phrase_cache' and len(requests) == 2
    tts.client.reference_text = 'A different reference transcript.'              # different voice: never reuse
    asyncio.run(collect(tts, utterance))
    assert len(requests) == 4


def test_live_synthesis_respects_the_first_audio_budget(tmp_path, pack):
    async def slow(request):
        await asyncio.sleep(0.5)
        return httpx.Response(200, content=fish_wav())

    tts = make_tts(tmp_path, slow, pack, live=True, budget=0.1)
    with pytest.raises(TtsUnavailable, match='budget'):
        asyncio.run(collect(tts, Utterance('Please hold.')))


def test_streaming_live_synthesis_converts_chunks_incrementally(tmp_path, pack):
    audio = fish_wav(0.5)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield audio[:44 + 8820]
            yield audio[44 + 8820:]

    tts = make_tts(tmp_path, lambda r: httpx.Response(200, stream=Chunks()), pack, live=True, streaming=True)
    pcm, stats = asyncio.run(collect(tts, Utterance('Thank you.')))
    assert stats.source == 'fish_live' and stats.first_audio_ms is not None
    assert abs(len(pcm) / 2 / 16000 - 0.5) < 0.01


def test_sentence_segmentation_for_incremental_synthesis():
    assert split_sentences('Hello there. How are you? Fine!') == ['Hello there.', 'How are you?', 'Fine!']
    long = 'We are open Monday to Saturday, from ten to eight, ' * 6
    assert all(len(chunk) <= 180 for chunk in split_sentences(long))


@pytest.mark.skipif(os.environ.get('FISH_SPEECH_LIVE_TEST') != '1', reason='set FISH_SPEECH_LIVE_TEST=1 with Fish Speech running')
def test_real_fish_speech_server_produces_valid_reference_voice_audio():
    client = FishSpeechClient(base_url=settings.fish_speech_base_url, reference_audio=REFERENCE,
                              reference_text=settings.fish_speech_reference_text, timeout_seconds=1800)
    health = asyncio.run(client.health(request_timeout=10))
    assert health.ready, health
    result = asyncio.run(client.synthesize('Thank you.'))
    assert result.fmt.channels == 1 and result.fmt.sample_width == 2 and result.audio_seconds > 0.3
