"""Fish Speech static test and performance measurement (uses the running Fish Speech server).

    python scripts/measure_fish_speech.py                 # static test + streaming + 2 concurrent requests
    python scripts/measure_fish_speech.py --concurrency 0 # skip the concurrency test

1. Static test: "Hello, thank you for calling Mouth Care Solutions." -> Fish Speech -> WAV,
    verified (format, duration, speech, ASR transcript) and converted to telephone PCM
   format (16-bit signed little-endian mono PCM, 20 ms frames).
2. Latency: non-streaming and streaming synthesis, time to first audio, real-time factor
   (synthesis_time / audio_duration), Fish server CPU and memory while it works.
3. Concurrency: N simultaneous short requests (Fish Speech 1.5.1 serialises generation).

Writes diagnostics/fish_speech_performance_<timestamp>.json and the audio it produced.
Never claims real time: the verdict compares measured numbers with the phone budget.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.core.config import settings  # noqa: E402
from backend.app.voice.audio_adapter import (  # noqa: E402
    FrameChunker,
    StreamingPcmConverter,
    convert_pcm,
    encode_wav,
    l16,
    rms_dbfs,
)
from backend.app.voice.fish_speech import FishParams, FishSpeechClient  # noqa: E402

STATIC_TEXT = 'Hello, thank you for calling Mouth Care Solutions.'
SHORT_TEXT = 'Thank you.'
PHONE_FIRST_AUDIO_BUDGET_S = 1.5


class ProcessSampler:
    def __init__(self, port: int):
        self.samples: list[tuple[float, float]] = []
        self.process = next((psutil.Process(c.pid) for c in psutil.net_connections('tcp')
                             if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN), None)
        self._stop = threading.Event()

    def __enter__(self):
        if self.process:
            self.process.cpu_percent(None)
            threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        while not self._stop.wait(1.0):
            try:
                self.samples.append((self.process.cpu_percent(None), self.process.memory_info().rss / 2**20))
            except psutil.Error:
                return

    def __exit__(self, *exc):
        self._stop.set()

    def summary(self) -> dict:
        if not self.samples:
            return {'cpu_percent_avg': None, 'cpu_percent_max': None, 'rss_mb_max': None}
        cpu = [s[0] for s in self.samples]
        return {'cpu_percent_avg': round(sum(cpu) / len(cpu), 1), 'cpu_percent_max': round(max(cpu), 1),
                'rss_mb_max': round(max(s[1] for s in self.samples), 1),
                'cpu_cores_busy_avg': round(sum(cpu) / len(cpu) / 100, 2)}


def client() -> FishSpeechClient:
    return FishSpeechClient(
        base_url=settings.fish_speech_base_url, api_key=settings.fish_speech_api_key, timeout_seconds=3600,
        reference_audio=settings.project_path(settings.fish_speech_reference_audio),
        reference_text=settings.fish_speech_reference_text, reference_id=settings.fish_speech_reference_id,
        params=FishParams(chunk_length=settings.fish_speech_chunk_length, max_new_tokens=settings.fish_speech_max_new_tokens,
                          top_p=settings.fish_speech_top_p, repetition_penalty=settings.fish_speech_repetition_penalty,
                          temperature=settings.fish_speech_temperature, seed=settings.fish_speech_seed),
        model=settings.fish_speech_model, version=settings.fish_speech_version,
        expected_sample_rate=settings.fish_speech_sample_rate)


async def static_test(fish: FishSpeechClient, out_dir: Path, stamp: str, asr_model: str) -> dict:
    with ProcessSampler(8080) as sampler:
        result = await fish.synthesize(STATIC_TEXT)
    wav_path = out_dir / f'fish_static_test_{stamp}.wav'
    wav_path.write_bytes(encode_wav(result.pcm, result.fmt))
    started = time.perf_counter()
    stream_fmt = l16(8000)
    pcm16k = convert_pcm(result.pcm, result.fmt, stream_fmt)
    chunker = FrameChunker(stream_fmt.chunk_bytes(20))
    frames = chunker.push(pcm16k) + chunker.flush()
    conversion_ms = (time.perf_counter() - started) * 1000
    (out_dir / f'fish_static_test_{stamp}_l16_{stream_fmt.sample_rate}.raw').write_bytes(b''.join(frames))
    transcript = ''
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(asr_model, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(str(wav_path), language='en', beam_size=5)
        transcript = ' '.join(s.text.strip() for s in segments)
    except Exception as exc:  # report, do not hide
        transcript = f'ASR unavailable: {type(exc).__name__}'
    words = transcript.lower()
    return {
        'text': STATIC_TEXT, 'wav': str(wav_path.relative_to(ROOT)),
        'fish_output_format': {'sample_rate': result.fmt.sample_rate, 'channels': result.fmt.channels,
                               'sample_width_bits': result.fmt.sample_width * 8, 'signed': result.fmt.signed,
                               'byteorder': result.fmt.byteorder, 'container': 'WAV (RIFF, PCM)'},
        'audio_seconds': round(result.audio_seconds, 3), 'synthesis_seconds': round(result.synthesis_seconds, 2),
        'real_time_factor': round(result.real_time_factor, 2), 'level_dbfs': round(rms_dbfs(result.pcm), 1),
        'asr_model': f'faster-whisper/{asr_model}', 'asr_transcript': transcript,
        'asr_matches': all(w in words for w in ('thank', 'calling', 'mouth', 'care')),
        'telephone_conversion': {'content_type': f'audio/l16;rate={stream_fmt.sample_rate}', 'frames': len(frames),
                              'frame_bytes': sorted({len(f) for f in frames}), 'conversion_ms': round(conversion_ms, 1),
                              'seconds': round(stream_fmt.duration_seconds(len(frames) * stream_fmt.chunk_bytes(20)), 3)},
        'fish_process': sampler.summary(),
    }


async def streaming_test(fish: FishSpeechClient) -> dict:
    stream_fmt = l16(8000)
    started = time.perf_counter()
    first_pcm = None
    total = 0
    chunks = 0
    converter = None
    with ProcessSampler(8080) as sampler:
        async for fmt, pcm in fish.stream(STATIC_TEXT):
            chunks += 1
            converter = converter or StreamingPcmConverter(fmt, stream_fmt)
            converted = converter.push(pcm)
            if first_pcm is None and converted:
                first_pcm = time.perf_counter() - started
            total += len(pcm)
            source_fmt = fmt
    elapsed = time.perf_counter() - started
    audio = source_fmt.duration_seconds(total) if chunks else 0.0
    return {'text': STATIC_TEXT, 'chunks': chunks, 'time_to_first_audio_seconds': round(first_pcm or elapsed, 2),
            'synthesis_seconds': round(elapsed, 2), 'audio_seconds': round(audio, 3),
            'real_time_factor': round(elapsed / audio, 2) if audio else None, 'fish_process': sampler.summary(),
            'note': 'Fish Speech 1.5.1 streams one PCM segment per generated text chunk, not per token.'}


async def concurrency_test(fish: FishSpeechClient, n: int) -> dict:
    async def one(i):
        started = time.perf_counter()
        result = await fish.synthesize(SHORT_TEXT)
        return {'request': i, 'latency_seconds': round(time.perf_counter() - started, 2),
                'audio_seconds': round(result.audio_seconds, 3)}

    started = time.perf_counter()
    with ProcessSampler(8080) as sampler:
        results = await asyncio.gather(*(one(i) for i in range(n)))
    return {'concurrent_requests': n, 'text': SHORT_TEXT, 'wall_seconds': round(time.perf_counter() - started, 2),
            'requests': results, 'fish_process': sampler.summary(),
            'note': 'the client allows parallel requests here; Fish Speech 1.5.1 queues generation internally'}


async def main_async(args) -> int:
    out_dir = ROOT / 'diagnostics'
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    fish = client()
    health = await fish.health(request_timeout=10)
    print('HEALTH', json.dumps(health.as_dict()))
    if not health.ready:
        print('FAIL: Fish Speech is not ready (start it with scripts/run_fish_speech.ps1)')
        return 1
    try:
        import torch  # noqa: F401  (only present in the Fish venv)
        cuda = 'see Fish venv'
    except ImportError:
        cuda = 'not importable here'
    report = {'created_at': datetime.now().isoformat(timespec='seconds'), 'fish_speech': health.as_dict(),
              'host': {'machine': platform.machine(), 'processor': platform.processor(), 'logical_cpus': psutil.cpu_count(),
                       'physical_cpus': psutil.cpu_count(logical=False),
                       'ram_gb': round(psutil.virtual_memory().total / 2**30, 1),
                       'torch_in_this_env': cuda}}
    print('STATIC_TEST ...', flush=True)
    report['static_test'] = await static_test(fish, out_dir, stamp, args.asr_model)
    print('STATIC_TEST', json.dumps(report['static_test']), flush=True)
    if args.streaming:
        print('STREAMING_TEST ...', flush=True)
        report['streaming_test'] = await streaming_test(fish)
        print('STREAMING_TEST', json.dumps(report['streaming_test']), flush=True)
    if args.concurrency > 1:
        print(f'CONCURRENCY_TEST n={args.concurrency} ...', flush=True)
        fish._max_concurrency = args.concurrency
        report['concurrency_test'] = await concurrency_test(fish, args.concurrency)
        print('CONCURRENCY_TEST', json.dumps(report['concurrency_test']), flush=True)
    static = report['static_test']
    ttfa = report.get('streaming_test', {}).get('time_to_first_audio_seconds', static['synthesis_seconds'])
    real_time = static['real_time_factor'] < 1.0 and ttfa <= PHONE_FIRST_AUDIO_BUDGET_S
    report['verdict'] = {
        'static_test_pass': static['asr_matches'] and static['audio_seconds'] > 1.0,
        'real_time_capable': real_time,
        'phone_first_audio_budget_seconds': PHONE_FIRST_AUDIO_BUDGET_S,
        'recommendation': ('live synthesis can serve phone turns' if real_time
                           else 'keep FISH_SPEECH_LIVE_SYNTHESIS=false and serve calls from the pre-generated Fish voice pack; '
                                'live synthesis needs a CUDA GPU host'),
    }
    path = out_dir / f'fish_speech_performance_{stamp}.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('VERDICT', json.dumps(report['verdict']))
    print('REPORT', path.relative_to(ROOT))
    return 0 if report['verdict']['static_test_pass'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--no-streaming', dest='streaming', action='store_false')
    parser.add_argument('--asr-model', default='small.en', help='intelligibility check model (voice-pack validator default)')
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == '__main__':
    sys.exit(main())
