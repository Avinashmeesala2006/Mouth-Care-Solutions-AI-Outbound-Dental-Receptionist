"""Audio format adapter between Fish Speech and telephone audio.

Every conversion is explicit about sample rate, channel count, sample width, signedness
and byte order. Formats in this system:

* Telephone PCM: raw ``audio/l16;rate=N`` - 16-bit **signed
  little-endian** linear PCM, mono, N in {8000, 16000, 24000}, one 20 ms frame per binary
  message (no WAV header, no base64, no compression).
* Fish Speech 1.5.1 ``/v1/tts``: WAV (RIFF) 16-bit PCM mono at the decoder rate reported
  in the WAV header (44100 Hz for firefly-gan-vq-fsq-8x1024-21hz). Streaming responses
  start with a header whose data size is 0, followed by raw PCM segments.
* Packaged voice assets: WAV 16-bit PCM mono 8000 Hz.

Resampling uses libsoxr (``soxr``) with its streaming resampler for chunked audio, so
chunk boundaries do not introduce clicks.
"""
from __future__ import annotations

import io
import math
import re
import struct
import wave
from dataclasses import dataclass

import numpy as np
import soxr

FRAME_MS = 20
_L16_CT = re.compile(r'^\s*audio/l16\s*;\s*rate\s*=\s*(\d+)\s*$', re.IGNORECASE)


class AudioFormatError(ValueError):
    """Audio is malformed or in a format this adapter does not support."""


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int = 1
    sample_width: int = 2          # bytes per sample
    signed: bool = True
    byteorder: str = 'little'      # 'little' | 'big'

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.channels <= 0 or self.sample_width not in (1, 2, 3, 4):
            raise AudioFormatError(f'unsupported audio format {self}')
        if self.byteorder not in ('little', 'big'):
            raise AudioFormatError('byteorder must be little or big')

    @property
    def bytes_per_frame(self) -> int:        # one sample for every channel
        return self.channels * self.sample_width

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.bytes_per_frame

    def chunk_bytes(self, duration_ms: int = FRAME_MS) -> int:
        samples = self.sample_rate * duration_ms // 1000
        return samples * self.bytes_per_frame

    def duration_seconds(self, pcm: bytes | int) -> float:
        size = pcm if isinstance(pcm, int) else len(pcm)
        return size / self.bytes_per_second


def l16(sample_rate: int) -> AudioFormat:
    """Telephone audio: 16-bit signed little-endian mono PCM."""
    return AudioFormat(sample_rate=sample_rate, channels=1, sample_width=2, signed=True, byteorder='little')


def parse_l16_content_type(value: str) -> AudioFormat:
    match = _L16_CT.match(value or '')
    if not match or int(match.group(1)) not in (8000, 16000, 24000):
        raise AudioFormatError(f'unsupported L16 content-type {value!r}')
    return l16(int(match.group(1)))


def l16_content_type(fmt: AudioFormat) -> str:
    if fmt != l16(fmt.sample_rate) or fmt.sample_rate not in (8000, 16000, 24000):
        raise AudioFormatError('Telephone audio accepts only 16-bit signed little-endian mono PCM at 8/16/24 kHz')
    return f'audio/l16;rate={fmt.sample_rate}'


# WAV --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WavHeader:
    fmt: AudioFormat
    data_offset: int
    data_size: int | None   # None: unknown/streaming (size field 0 or 0xFFFFFFFF)


def parse_wav_header(data: bytes) -> WavHeader | None:
    """Parse a RIFF/WAVE header from the start of ``data``.

    Returns None when more bytes are needed. Raises AudioFormatError when the bytes are
    not an uncompressed PCM WAV (compressed or float WAVs are rejected, never guessed).
    """
    if len(data) < 12:
        return None
    if data[:4] not in (b'RIFF', b'RIFX') or data[8:12] != b'WAVE':
        raise AudioFormatError('not a RIFF/WAVE stream')
    big_endian = data[:4] == b'RIFX'
    endian = '>' if big_endian else '<'
    offset = 12
    fmt: AudioFormat | None = None
    while True:
        if len(data) < offset + 8:
            return None
        chunk_id = data[offset:offset + 4]
        (chunk_size,) = struct.unpack(endian + 'I', data[offset + 4:offset + 8])
        body = offset + 8
        if chunk_id == b'fmt ':
            if len(data) < body + 16:
                return None
            audio_format, channels, rate, _byte_rate, _block_align, bits = struct.unpack(endian + 'HHIIHH', data[body:body + 16])
            if audio_format == 0xFFFE and chunk_size >= 40:     # WAVE_FORMAT_EXTENSIBLE: sub-format GUID
                if len(data) < body + 26:
                    return None
                (audio_format,) = struct.unpack(endian + 'H', data[body + 24:body + 26])
            if audio_format != 1:
                raise AudioFormatError(f'WAV encoding {audio_format} is not linear PCM')
            if bits not in (8, 16, 24, 32):
                raise AudioFormatError(f'unsupported WAV bit depth {bits}')
            fmt = AudioFormat(sample_rate=rate, channels=channels, sample_width=bits // 8, signed=bits != 8,
                              byteorder='big' if big_endian else 'little')
        elif chunk_id == b'data':
            if fmt is None:
                raise AudioFormatError('WAV data chunk before fmt chunk')
            size = None if chunk_size in (0, 0xFFFFFFFF) else chunk_size
            return WavHeader(fmt=fmt, data_offset=body, data_size=size)
        offset = body + chunk_size + (chunk_size & 1)


def decode_wav(data: bytes) -> tuple[AudioFormat, bytes]:
    """Decode a complete PCM WAV into (format, raw PCM). The header is never kept."""
    header = parse_wav_header(data)
    if header is None:
        raise AudioFormatError('truncated WAV header')
    end = len(data) if header.data_size is None else min(len(data), header.data_offset + header.data_size)
    pcm = data[header.data_offset:end]
    usable = len(pcm) - len(pcm) % header.fmt.bytes_per_frame
    if usable <= 0:
        raise AudioFormatError('WAV contains no audio frames')
    return header.fmt, pcm[:usable]


def encode_wav(pcm: bytes, fmt: AudioFormat) -> bytes:
    if fmt.byteorder != 'little' or (fmt.sample_width > 1 and not fmt.signed):
        pcm = convert_pcm(pcm, fmt, AudioFormat(fmt.sample_rate, fmt.channels, 2))
        fmt = AudioFormat(fmt.sample_rate, fmt.channels, 2)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(fmt.channels)
        out.setsampwidth(fmt.sample_width)
        out.setframerate(fmt.sample_rate)
        out.writeframes(pcm)
    return buffer.getvalue()


# Sample conversion ------------------------------------------------------------------------
def pcm_to_float(pcm: bytes, fmt: AudioFormat) -> np.ndarray:
    """PCM bytes -> float32 array shaped (frames, channels) in [-1, 1]."""
    usable = len(pcm) - len(pcm) % fmt.bytes_per_frame
    raw = np.frombuffer(pcm[:usable], dtype=np.uint8)
    order = '<' if fmt.byteorder == 'little' else '>'
    if fmt.sample_width == 1:
        values = raw.astype(np.float32)
        values = (values - 128.0) / 128.0 if not fmt.signed else raw.view(np.int8).astype(np.float32) / 128.0
    elif fmt.sample_width == 2:
        values = np.frombuffer(pcm[:usable], dtype=order + ('i2' if fmt.signed else 'u2')).astype(np.float32)
        values = values / 32768.0 if fmt.signed else (values - 32768.0) / 32768.0
    elif fmt.sample_width == 3:
        triples = raw.reshape(-1, 3).astype(np.int32)
        if fmt.byteorder == 'little':
            ints = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        else:
            ints = triples[:, 2] | (triples[:, 1] << 8) | (triples[:, 0] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints) if fmt.signed else ints - 0x800000
        values = ints.astype(np.float32) / 8388608.0
    else:
        values = np.frombuffer(pcm[:usable], dtype=order + ('i4' if fmt.signed else 'u4')).astype(np.float64)
        values = (values / 2147483648.0 if fmt.signed else (values - 2147483648.0) / 2147483648.0).astype(np.float32)
    return values.reshape(-1, fmt.channels)


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """float32 mono in [-1, 1] -> 16-bit signed little-endian PCM (clipped, rounded)."""
    clipped = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 32767.0 / 32768.0)
    return np.round(clipped * 32768.0).astype('<i2').tobytes()


def _pcm16_mono(pcm: bytes, fmt: AudioFormat) -> np.ndarray:
    """Any PCM -> int16 mono array (little-endian semantics) at the source rate."""
    if fmt.channels == 1 and fmt.sample_width == 2 and fmt.signed and fmt.byteorder == 'little':
        usable = len(pcm) - len(pcm) % 2
        return np.frombuffer(pcm[:usable], dtype='<i2').astype(np.int16)
    mono = pcm_to_float(pcm, fmt).mean(axis=1)
    return np.frombuffer(float_to_pcm16(mono), dtype='<i2').astype(np.int16)


def convert_pcm(pcm: bytes, src: AudioFormat, dst: AudioFormat) -> bytes:
    """Convert complete PCM between formats (mono mixdown, width, endianness, resampling).

    Only mono 16-bit signed output is produced by the voice path; other targets are
    supported for tests and diagnostics through the float path.
    """
    if src == dst:
        return pcm[: len(pcm) - len(pcm) % src.bytes_per_frame]
    if dst.channels != 1:
        raise AudioFormatError('only mono output is supported')
    samples = _pcm16_mono(pcm, src)
    if src.sample_rate != dst.sample_rate and samples.size:
        samples = soxr.resample(samples, src.sample_rate, dst.sample_rate, quality='HQ')
    return _encode_from_int16(samples, dst)


def _encode_from_int16(samples: np.ndarray, dst: AudioFormat) -> bytes:
    if dst.sample_width == 2 and dst.signed:
        return np.asarray(samples, dtype=np.int16).astype('<i2' if dst.byteorder == 'little' else '>i2').tobytes()
    floats = np.asarray(samples, dtype=np.float32) / 32768.0
    if dst.sample_width == 1:
        data = np.round(floats * 127.0).astype(np.int8)
        return (data.astype(np.int16) + 128).astype(np.uint8).tobytes() if not dst.signed else data.tobytes()
    raise AudioFormatError(f'unsupported output format {dst}')


class StreamingPcmConverter:
    """Chunked conversion to 16-bit signed little-endian mono at ``dst`` rate.

    Keeps partial samples between chunks and uses a stateful soxr resampler, so audio
    streamed in arbitrary chunk sizes converts exactly like the complete signal.
    """

    def __init__(self, src: AudioFormat, dst: AudioFormat):
        if dst != l16(dst.sample_rate):
            raise AudioFormatError('streaming target must be 16-bit signed little-endian mono PCM')
        self.src, self.dst = src, dst
        self._carry = b''
        self._stream = (soxr.ResampleStream(src.sample_rate, dst.sample_rate, 1, dtype='int16', quality='HQ')
                        if src.sample_rate != dst.sample_rate else None)

    def push(self, data: bytes, *, last: bool = False) -> bytes:
        data = self._carry + data
        usable = len(data) - len(data) % self.src.bytes_per_frame
        self._carry = data[usable:]
        samples = _pcm16_mono(data[:usable], self.src) if usable else np.zeros(0, dtype=np.int16)
        if self._stream is not None:
            samples = self._stream.resample_chunk(samples, last=last)
        return np.asarray(samples, dtype=np.int16).astype('<i2').tobytes()

    def flush(self) -> bytes:
        return self.push(b'', last=True)


class FrameChunker:
    """Splits PCM into fixed-size telephone frames (20 ms); the last frame is padded
    with silence so an incomplete packet is never sent."""

    def __init__(self, frame_bytes: int):
        if frame_bytes <= 0 or frame_bytes % 2:
            raise AudioFormatError('frame size must be a positive even number of bytes')
        self.frame_bytes = frame_bytes
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._buffer.extend(pcm)
        count = len(self._buffer) // self.frame_bytes
        frames = [bytes(self._buffer[i * self.frame_bytes:(i + 1) * self.frame_bytes]) for i in range(count)]
        del self._buffer[:count * self.frame_bytes]
        return frames

    def flush(self) -> list[bytes]:
        if not self._buffer:
            return []
        frame = bytes(self._buffer) + b'\x00' * (self.frame_bytes - len(self._buffer))
        self._buffer.clear()
        return [frame]

    def reset(self) -> None:
        self._buffer.clear()


def frames_of(pcm: bytes, frame_bytes: int) -> list[bytes]:
    chunker = FrameChunker(frame_bytes)
    return chunker.push(pcm) + chunker.flush()


def rms_dbfs(pcm16: bytes) -> float:
    """Level of 16-bit signed little-endian PCM in dBFS (-inf for digital silence)."""
    usable = len(pcm16) - len(pcm16) % 2
    if not usable:
        return float('-inf')
    samples = np.frombuffer(pcm16[:usable], dtype='<i2').astype(np.float64)
    rms = math.sqrt(float(np.mean(samples * samples)))
    return 20 * math.log10(rms / 32768.0) if rms > 0 else float('-inf')


def silence(duration_ms: int, fmt: AudioFormat) -> bytes:
    return b'\x00' * fmt.chunk_bytes(duration_ms)

