"""Fish Speech local TTS adapter.

The adapter targets the current Fish Speech local API server (`POST /v1/tts`).
It keeps model lifetime outside this application: Fish Speech's server loads the
model once and this process sends text requests to it.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import subprocess
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

try:
    import ormsgpack
except ModuleNotFoundError:  # pragma: no cover - installed in the dedicated Fish runtime env
    ormsgpack = None

logger = logging.getLogger(__name__)


class FishSpeechError(RuntimeError):
    """Raised when Fish Speech cannot produce usable audio."""


@dataclass(frozen=True)
class AudioArtifact:
    audio_id: str
    content: bytes
    content_type: str
    sample_rate: int
    channels: int
    duration_seconds: float
    generation_seconds: float
    source: str = "fish-speech"


class AudioStore:
    """Small TTL store for generated audio fetched by id."""

    def __init__(self, ttl_seconds: int = 300, max_items: int = 100):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[str, tuple[float, AudioArtifact]] = {}
        self._static_items: dict[str, AudioArtifact] = {}
        self._lock = asyncio.Lock()

    def add_static(self, artifact: AudioArtifact) -> str:
        """Register a pre-generated artifact that must survive beyond the TTL."""
        self._static_items[artifact.audio_id] = artifact
        return artifact.audio_id

    async def put(self, artifact: AudioArtifact) -> str:
        async with self._lock:
            now = time.monotonic()
            self._items = {
                key: value
                for key, value in self._items.items()
                if value[0] > now
            }
            while len(self._items) >= self.max_items:
                self._items.pop(next(iter(self._items)))
            self._items[artifact.audio_id] = (now + self.ttl_seconds, artifact)
        return artifact.audio_id

    async def get(self, audio_id: str) -> AudioArtifact | None:
        async with self._lock:
            static_artifact = self._static_items.get(audio_id)
            if static_artifact:
                return static_artifact
            item = self._items.get(audio_id)
            if not item:
                return None
            expires_at, artifact = item
            if expires_at <= time.monotonic():
                self._items.pop(audio_id, None)
                return None
            return artifact


class FishSpeechAdapter:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        output_format: str = "wav",
        telephone_sample_rate: int = 8000,
        telephone_channels: int = 1,
        max_new_tokens: int = 256,
        reference_audio: str = "",
        reference_text: str = "",
        reference_id: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.output_format = output_format
        self.telephone_sample_rate = telephone_sample_rate
        self.telephone_channels = telephone_channels
        self.max_new_tokens = max_new_tokens
        self.reference_audio = reference_audio
        self.reference_text = reference_text
        self.reference_id = reference_id

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/tts"

    def _payload(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("tts_text_required")
        if len(text) > 2000:
            raise ValueError("tts_text_too_long")

        payload = {
            "text": text.strip(),
            "format": self.output_format,
            "streaming": False,
            "normalize": True,
            "chunk_length": 200,
            "max_new_tokens": self.max_new_tokens,
        }
        if self.reference_id:
            payload["reference_id"] = self.reference_id
        elif self.reference_audio:
            raw = Path(self.reference_audio).read_bytes()
            payload["references"] = [{
                "audio": raw,
                "text": self.reference_text or text.strip(),
            }]
        return payload

    def _msgpack_request(self, text: str) -> bytes:
        if ormsgpack is None:
            raise RuntimeError("ormsgpack_not_installed")
        payload = self._payload(text)
        return ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_PYDANTIC)

    async def synthesize(self, text: str) -> AudioArtifact:
        try:
            request_bytes = self._msgpack_request(text)
        except OSError as exc:
            logger.warning("Fish Speech reference audio could not be read: %s", type(exc).__name__)
            raise FishSpeechError("fish_speech_reference_unavailable") from exc
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    data=request_bytes,
                    headers={"content-type": "application/msgpack"},
                )
                response.raise_for_status()
                raw_audio = response.content
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Fish Speech request failed: %s", type(exc).__name__)
            raise FishSpeechError("fish_speech_unavailable") from exc

        if not raw_audio:
            raise FishSpeechError("fish_speech_empty_audio")
        try:
            phone_audio = await asyncio.to_thread(self._to_telephone_wav, raw_audio)
            sample_rate, channels, duration = self._validate_wav(phone_audio)
        except (OSError, ValueError, wave.Error, subprocess.SubprocessError) as exc:
            logger.warning("Fish Speech audio validation failed: %s", type(exc).__name__)
            raise FishSpeechError("fish_speech_invalid_audio") from exc

        elapsed = time.perf_counter() - started
        artifact = AudioArtifact(
            audio_id=uuid.uuid4().hex,
            content=phone_audio,
            content_type="audio/wav",
            sample_rate=sample_rate,
            channels=channels,
            duration_seconds=duration,
            generation_seconds=elapsed,
        )
        logger.info(
            "fish_speech_generated audio_id=%s chars=%d duration=%.3fs latency=%.3fs rtf=%.3f",
            artifact.audio_id,
            len(text),
            duration,
            elapsed,
            elapsed / duration if duration else 0,
        )
        return artifact

    def _to_telephone_wav(self, raw_audio: bytes) -> bytes:
        """Convert Fish audio to 8 kHz mono 16-bit PCM WAV for telephone playback."""
        with tempfile.TemporaryDirectory(prefix="mcs-fish-") as directory:
            source = os.path.join(directory, "source.audio")
            target = os.path.join(directory, "telephone.wav")
            Path(source).write_bytes(raw_audio)
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", source,
                "-ar", str(self.telephone_sample_rate),
                "-ac", str(self.telephone_channels),
                "-c:a", "pcm_s16le",
                target,
            ]
            subprocess.run(command, check=True, capture_output=True, timeout=20)
            return Path(target).read_bytes()

    @staticmethod
    def _validate_wav(content: bytes) -> tuple[int, int, float]:
        with wave.open(io.BytesIO(content), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frames = wav_file.getnframes()
            if sample_rate != 8000 or channels != 1 or sample_width != 2:
                raise ValueError("telephone_wav_format_invalid")
            if frames <= 0:
                raise ValueError("telephone_wav_empty")
            return sample_rate, channels, frames / sample_rate
