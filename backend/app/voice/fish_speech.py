"""Fish Speech HTTP client for the installed Fish Speech **v1.5.1** API server.

Verified against ``tools/server/views.py`` of the v1.5.1 checkout:

* ``GET /v1/health`` -> ``{"status": "ok"}``. The server binds its port only after the
  models are loaded (uvicorn startup hook), so a healthy response implies a loaded model.
* ``POST /v1/tts`` with an ``application/msgpack`` (or JSON) ``ServeTTSRequest``:
  ``text``, ``references`` ([{audio: bytes, text}]) or ``reference_id``, ``format``,
  ``streaming``, ``chunk_length`` (100-300), ``max_new_tokens``, ``top_p``,
  ``repetition_penalty``, ``temperature``, ``seed``, ``normalize``, ``use_memory_cache``.
* Output is WAV, 16-bit PCM mono at the decoder rate (44100 Hz for fish-speech-1.5).
  With ``streaming=true`` v1.5.1 returns raw 16-bit PCM segments, one per generated text
  chunk (not per token), and **no WAV header** (verified: the header is dropped server-side).

``use_memory_cache="on"`` makes the server reuse the encoded reference voice (VQ tokens)
across requests instead of re-encoding the reference audio every time.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import ormsgpack

from .audio_adapter import AudioFormat, AudioFormatError, decode_wav, parse_wav_header

logger = logging.getLogger(__name__)
HEALTH_PATH = '/v1/health'
TTS_PATH = '/v1/tts'
MAX_TEXT_CHARS = 2000


class FishSpeechError(RuntimeError):
    """Fish Speech could not produce usable audio. ``code`` is a short, safe category."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code


@dataclass(frozen=True)
class FishParams:
    chunk_length: int = 200
    max_new_tokens: int = 1024
    top_p: float = 0.7
    repetition_penalty: float = 1.2
    temperature: float = 0.7
    seed: int | None = None
    normalize: bool = True


@dataclass(frozen=True)
class FishHealth:
    enabled: bool
    configured: bool
    reachable: bool
    model_loaded: bool
    ready: bool
    reference_ready: bool
    latency_ms: int | None
    error: str | None
    model: str
    version: str
    private_endpoint: bool

    def as_dict(self) -> dict:
        return {'provider': 'fish-speech', 'enabled': self.enabled, 'configured': self.configured,
                'reachable': self.reachable, 'model_loaded': self.model_loaded, 'ready': self.ready,
                'reference_ready': self.reference_ready, 'latency_ms': self.latency_ms, 'error': self.error,
                'model': self.model, 'version': self.version, 'private_endpoint': self.private_endpoint}


@dataclass(frozen=True)
class SynthesisResult:
    text: str
    fmt: AudioFormat
    pcm: bytes
    synthesis_seconds: float
    first_audio_seconds: float

    @property
    def audio_seconds(self) -> float:
        return self.fmt.duration_seconds(self.pcm)

    @property
    def real_time_factor(self) -> float:
        return self.synthesis_seconds / self.audio_seconds if self.audio_seconds else float('inf')


class FishSpeechClient:
    def __init__(self, *, base_url: str, enabled: bool = True, api_key: str = '', timeout_seconds: float = 900.0,
                 reference_audio: Path | None = None, reference_text: str = '', reference_id: str = '',
                 params: FishParams | None = None, model: str = 'fish-speech-1.5', version: str = 'v1.5.1',
                 expected_sample_rate: int | None = None, max_concurrency: int = 1,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip('/')
        self.enabled = enabled
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.reference_audio = reference_audio
        self.reference_text = reference_text
        self.reference_id = reference_id
        self.params = params or FishParams()
        self.model = model
        self.version = version
        self.expected_sample_rate = expected_sample_rate
        self._transport = transport
        self._max_concurrency = max(1, max_concurrency)
        self._semaphore: asyncio.Semaphore | None = None
        self._reference_cache: tuple[float, bytes, str] | None = None   # (mtime, audio, sha256)
        self.last_output_format: AudioFormat | None = None

    # Configuration ----------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.base_url) and (bool(self.reference_id) or self.reference_ready)

    @property
    def reference_ready(self) -> bool:
        return bool(self.reference_audio and self.reference_audio.is_file() and self.reference_text.strip())

    @property
    def private_endpoint(self) -> bool:
        host = (urlparse(self.base_url).hostname or '').lower()
        return host in {'127.0.0.1', 'localhost', '::1'} or host.startswith(('10.', '192.168.')) or '.' not in host

    def reference_sha256(self) -> str | None:
        if self._reference_bytes(optional=True) is None or self._reference_cache is None:
            return None
        return self._reference_cache[2]

    def voice_signature(self) -> dict:
        """Everything that changes the synthesized voice; part of every cache key."""
        return {'model': self.model, 'version': self.version, 'reference_sha256': self.reference_sha256(),
                'reference_text': self.reference_text if not self.reference_id else '', 'reference_id': self.reference_id,
                'params': {k: getattr(self.params, k) for k in FishParams.__dataclass_fields__}}

    def _reference_bytes(self, optional: bool = False) -> bytes | None:
        if not self.reference_audio:
            return None
        try:
            mtime = self.reference_audio.stat().st_mtime
            if self._reference_cache is None or self._reference_cache[0] != mtime:
                data = self.reference_audio.read_bytes()
                self._reference_cache = (mtime, data, hashlib.sha256(data).hexdigest())
            return self._reference_cache[1]
        except OSError as exc:
            if optional:
                return None
            raise FishSpeechError('reference_unavailable', 'reference audio could not be read') from exc

    def _payload(self, text: str, *, streaming: bool) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise FishSpeechError('invalid_text', 'text is required')
        if len(text) > MAX_TEXT_CHARS:
            raise FishSpeechError('invalid_text', 'text is too long')
        payload: dict = {'text': text.strip(), 'format': 'wav', 'streaming': streaming, 'use_memory_cache': 'on',
                         'chunk_length': self.params.chunk_length, 'max_new_tokens': self.params.max_new_tokens,
                         'top_p': self.params.top_p, 'repetition_penalty': self.params.repetition_penalty,
                         'temperature': self.params.temperature, 'normalize': self.params.normalize}
        if self.params.seed is not None:
            payload['seed'] = self.params.seed
        if self.reference_id:
            payload['reference_id'] = self.reference_id
        else:
            audio = self._reference_bytes()
            if not audio or not self.reference_text.strip():
                raise FishSpeechError('reference_unavailable', 'reference audio and transcript are required')
            payload['references'] = [{'audio': audio, 'text': self.reference_text}]
        return ormsgpack.packb(payload)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, transport=self._transport,
                                 timeout=httpx.Timeout(timeout, connect=5.0))

    def _check_format(self, fmt: AudioFormat) -> AudioFormat:
        if fmt.channels != 1 or fmt.sample_width != 2 or not fmt.signed:
            raise FishSpeechError('malformed_output', f'unexpected Fish Speech output format {fmt}')
        if self.expected_sample_rate and fmt.sample_rate != self.expected_sample_rate:
            logger.warning('fish_speech_sample_rate_differs expected=%s actual=%s (the WAV header is used)',
                           self.expected_sample_rate, fmt.sample_rate)
        self.last_output_format = fmt
        return fmt

    @staticmethod
    def _http_error(response: httpx.Response) -> FishSpeechError:
        if response.status_code == 401:
            return FishSpeechError('unauthorized', 'Fish Speech rejected the API key')
        if response.status_code >= 500:
            return FishSpeechError('inference_error', f'Fish Speech HTTP {response.status_code}: {response.text[:160]}')
        return FishSpeechError('http_error', f'Fish Speech HTTP {response.status_code}: {response.text[:160]}')

    async def _guarded(self):
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    # Health --------------------------------------------------------------------------------------
    async def health(self, request_timeout: float = 3.0) -> FishHealth:
        reachable = model_loaded = False
        latency = error = None
        if self.enabled and self.base_url:
            started = time.perf_counter()
            try:
                async with self._client(request_timeout) as client:
                    response = await client.get(HEALTH_PATH)
                latency = int((time.perf_counter() - started) * 1000)
                reachable = True
                if response.status_code == 200 and (response.json() or {}).get('status') == 'ok':
                    model_loaded = True
                else:
                    error = f'health returned HTTP {response.status_code}'
            except httpx.ConnectError:
                error = 'connection_refused'
            except httpx.TimeoutException:
                reachable, error = True, 'health_timeout (server busy synthesizing or unresponsive)'
            except (httpx.HTTPError, ValueError) as exc:
                error = type(exc).__name__
        elif not self.enabled:
            error = 'disabled'
        ready = self.enabled and model_loaded and self.configured
        return FishHealth(enabled=self.enabled, configured=self.configured, reachable=reachable, model_loaded=model_loaded,
                          ready=ready, reference_ready=self.reference_ready or bool(self.reference_id), latency_ms=latency,
                          error=error, model=self.model, version=self.version, private_endpoint=self.private_endpoint)

    # Synthesis -----------------------------------------------------------------------------------
    async def synthesize(self, text: str, *, request_timeout: float | None = None) -> SynthesisResult:
        """Non-streaming synthesis; returns the decoded PCM and its actual format."""
        if not self.enabled:
            raise FishSpeechError('disabled', 'FISH_SPEECH_ENABLED=false')
        body = self._payload(text, streaming=False)
        semaphore = await self._guarded()
        async with semaphore:
            started = time.perf_counter()
            try:
                async with self._client(request_timeout or self.timeout_seconds) as client:
                    response = await client.post(TTS_PATH, content=body, headers={'content-type': 'application/msgpack'})
            except httpx.ConnectError as exc:
                raise FishSpeechError('connection_refused', 'Fish Speech server is not reachable') from exc
            except httpx.TimeoutException as exc:
                raise FishSpeechError('timeout', 'Fish Speech did not answer in time') from exc
            except httpx.HTTPError as exc:
                raise FishSpeechError('unavailable', type(exc).__name__) from exc
            elapsed = time.perf_counter() - started
        if response.status_code != 200:
            raise self._http_error(response)
        try:
            fmt, pcm = decode_wav(response.content)
        except AudioFormatError as exc:
            raise FishSpeechError('malformed_output', str(exc)) from exc
        self._check_format(fmt)
        result = SynthesisResult(text=text, fmt=fmt, pcm=pcm, synthesis_seconds=elapsed, first_audio_seconds=elapsed)
        logger.info('fish_speech_synthesized chars=%d audio_s=%.2f synthesis_s=%.2f rtf=%.2f rate=%d',
                    len(text), result.audio_seconds, elapsed, result.real_time_factor, fmt.sample_rate)
        return result

    def _headerless_format(self) -> AudioFormat:
        """Format of a v1.5.1 streaming body, which carries no WAV header (see module docstring)."""
        rate = self.last_output_format.sample_rate if self.last_output_format else self.expected_sample_rate
        if not rate:
            raise FishSpeechError('malformed_output', 'headerless stream and unknown sample rate '
                                  '(set FISH_SPEECH_SAMPLE_RATE or run one non-streaming synthesis first)')
        return AudioFormat(sample_rate=rate, channels=1, sample_width=2, signed=True, byteorder='little')

    async def stream(self, text: str, *, request_timeout: float | None = None) -> AsyncGenerator[tuple[AudioFormat, bytes], None]:
        """Streaming synthesis: yields (format, PCM) chunks as Fish Speech produces them.

        A WAV header, when present, is parsed and stripped. Fish Speech 1.5.1 drops its own
        streaming header (it is yielded as a NumPy array and filtered out by
        ``inference_async``), so a body that does not start with ``RIFF`` is raw 16-bit mono
        PCM at the decoder rate: the rate learned from a real WAV response, else
        ``FISH_SPEECH_SAMPLE_RATE``; with neither known the stream is refused, never guessed.
        PCM chunks are aligned to whole samples.
        """
        if not self.enabled:
            raise FishSpeechError('disabled', 'FISH_SPEECH_ENABLED=false')
        body = self._payload(text, streaming=True)
        semaphore = await self._guarded()
        async with semaphore:
            try:
                async with self._client(request_timeout or self.timeout_seconds) as client:
                    async with client.stream('POST', TTS_PATH, content=body,
                                             headers={'content-type': 'application/msgpack'}) as response:
                        if response.status_code != 200:
                            await response.aread()
                            raise self._http_error(response)
                        buffer = b''
                        fmt: AudioFormat | None = None
                        async for chunk in response.aiter_bytes():
                            buffer += chunk
                            if fmt is None:
                                if len(buffer) < 4:
                                    continue
                                if buffer[:4] in (b'RIFF', b'RIFX'):
                                    header = parse_wav_header(buffer)
                                    if header is None:
                                        continue
                                    fmt = self._check_format(header.fmt)
                                    buffer = buffer[header.data_offset:]
                                else:
                                    fmt = self._headerless_format()
                            usable = len(buffer) - len(buffer) % fmt.bytes_per_frame
                            if usable:
                                yield fmt, buffer[:usable]
                                buffer = buffer[usable:]
                        if fmt is None:
                            raise FishSpeechError('malformed_output', 'stream ended before a WAV header')
            except AudioFormatError as exc:
                raise FishSpeechError('malformed_output', str(exc)) from exc
            except httpx.ConnectError as exc:
                raise FishSpeechError('connection_refused', 'Fish Speech server is not reachable') from exc
            except httpx.TimeoutException as exc:
                raise FishSpeechError('timeout', 'Fish Speech did not answer in time') from exc
            except httpx.HTTPError as exc:
                raise FishSpeechError('unavailable', type(exc).__name__) from exc
