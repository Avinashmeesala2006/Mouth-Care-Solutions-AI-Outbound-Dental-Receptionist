"""Audio format adapter: Fish Speech WAV and telephone PCM conversion."""
import io
import struct
import wave

import numpy as np
import pytest

from backend.app.voice import audio_adapter as a


def sine_pcm(freq, rate, seconds=1.0, amplitude=0.5, channels=1, width=2) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    x = amplitude * np.sin(2 * np.pi * freq * t)
    if width == 2:
        data = np.round(x * 32767).astype('<i2')
    elif width == 1:
        data = (np.round(x * 127) + 128).astype(np.uint8)
    else:
        raise ValueError(width)
    if channels == 2:
        data = np.repeat(data, 2)
    return data.tobytes()


def wav_bytes(pcm, rate, channels=1, width=2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def dominant_frequency(pcm16: bytes, rate: int) -> float:
    x = np.frombuffer(pcm16, '<i2').astype(float)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spectrum)])


def test_l16_content_types_are_parsed_exactly():
    assert a.parse_l16_content_type('audio/l16;rate=16000') == a.AudioFormat(16000, 1, 2, True, 'little')
    assert a.parse_l16_content_type(' audio/L16; rate=8000 ').sample_rate == 8000
    assert a.l16_content_type(a.l16(24000)) == 'audio/l16;rate=24000'
    for bad in ('audio/l16;rate=44100', 'audio/pcmu', 'audio/l16', ''):
        with pytest.raises(a.AudioFormatError):
            a.parse_l16_content_type(bad)
    with pytest.raises(a.AudioFormatError):
        a.l16_content_type(a.AudioFormat(16000, 2))


def test_frame_sizes_follow_rate_width_and_channels():
    assert a.l16(16000).chunk_bytes(20) == 640
    assert a.l16(8000).chunk_bytes(20) == 320
    assert a.l16(24000).chunk_bytes(20) == 960
    assert a.AudioFormat(44100, 2, 2).bytes_per_second == 176400


def test_fish_wav_is_decoded_without_its_header():
    pcm = sine_pcm(440, 44100, 0.5)
    fmt, decoded = a.decode_wav(wav_bytes(pcm, 44100))
    assert fmt == a.AudioFormat(44100, 1, 2, True, 'little')
    assert decoded == pcm and not decoded.startswith(b'RIFF')


def test_streaming_wav_header_with_unknown_size_is_parsed():
    header = wav_bytes(b'', 44100)          # Fish Speech streaming header: data size 0
    parsed = a.parse_wav_header(header)
    assert parsed.data_size is None and parsed.data_offset == 44 and parsed.fmt.sample_rate == 44100
    assert a.parse_wav_header(header[:20]) is None            # needs more bytes
    with pytest.raises(a.AudioFormatError):
        a.parse_wav_header(b'ID3\x03' + b'\x00' * 40)         # MP3 is never guessed


def test_non_pcm_and_truncated_wavs_are_rejected():
    riff = bytearray(wav_bytes(sine_pcm(440, 8000, 0.1), 8000))
    struct.pack_into('<H', riff, 20, 3)                        # IEEE float
    with pytest.raises(a.AudioFormatError, match='not linear PCM'):
        a.decode_wav(bytes(riff))
    with pytest.raises(a.AudioFormatError):
        a.decode_wav(wav_bytes(b'', 8000))
    with pytest.raises(a.AudioFormatError):
        a.decode_wav(b'RIFF')


def test_stereo_8bit_and_big_endian_sources_convert_to_l16():
    stereo = sine_pcm(300, 16000, 0.5, channels=2)
    out = a.convert_pcm(stereo, a.AudioFormat(16000, 2, 2), a.l16(16000))
    assert len(out) == len(stereo) // 2
    unsigned8 = sine_pcm(300, 8000, 0.5, width=1)
    out8 = a.convert_pcm(unsigned8, a.AudioFormat(8000, 1, 1, signed=False), a.l16(8000))
    assert abs(dominant_frequency(out8, 8000) - 300) < 5
    little = sine_pcm(500, 16000, 0.25)
    big = np.frombuffer(little, '<i2').astype('>i2').tobytes()
    assert a.convert_pcm(big, a.AudioFormat(16000, 1, 2, True, 'big'), a.l16(16000)) == little


@pytest.mark.parametrize('src_rate,dst_rate,freq', [(44100, 16000, 1000), (44100, 8000, 700), (8000, 16000, 440),
                                                     (24000, 16000, 3000), (16000, 24000, 2500)])
def test_resampling_preserves_pitch_and_duration(src_rate, dst_rate, freq):
    pcm = sine_pcm(freq, src_rate, 1.0)
    out = a.convert_pcm(pcm, a.l16(src_rate), a.l16(dst_rate))
    assert abs(len(out) / 2 - dst_rate) <= 2
    assert abs(dominant_frequency(out, dst_rate) - freq) < 3


def test_downsampling_removes_content_above_the_new_nyquist():
    tone = sine_pcm(7000, 44100, 1.0, amplitude=0.8)       # above 4 kHz: must not alias into the 8 kHz stream
    out = a.convert_pcm(tone, a.l16(44100), a.l16(8000))
    assert a.rms_dbfs(out[1600:-1600]) < -50


def test_streaming_conversion_matches_one_shot_for_arbitrary_chunks():
    pcm = sine_pcm(523, 44100, 1.3)
    src, dst = a.l16(44100), a.l16(16000)
    one = np.frombuffer(a.convert_pcm(pcm, src, dst), '<i2').astype(int)
    converter = a.StreamingPcmConverter(src, dst)
    rng = np.random.default_rng(3)
    parts, i = [], 0
    while i < len(pcm):
        n = int(rng.integers(1, 3001))                        # odd sizes split samples in half
        parts.append(converter.push(pcm[i:i + n]))
        i += n
    parts.append(converter.flush())
    streamed = np.frombuffer(b''.join(parts), '<i2').astype(int)
    assert len(streamed) == len(one)
    assert np.max(np.abs(streamed - one)) <= 2


def test_frame_chunker_never_emits_a_partial_packet():
    chunker = a.FrameChunker(640)
    assert chunker.push(b'\x01' * 1000) == [b'\x01' * 640]
    tail = chunker.flush()
    assert len(tail) == 1 and len(tail[0]) == 640 and tail[0].endswith(b'\x00' * 280)
    assert chunker.flush() == []
    with pytest.raises(a.AudioFormatError):
        a.FrameChunker(641)
    assert all(len(f) == 320 for f in a.frames_of(b'\x02' * 1000, 320))


def test_levels_and_silence():
    assert a.rms_dbfs(a.silence(20, a.l16(16000))) == float('-inf')
    # A sine with 0.5 peak has RMS 0.5/sqrt(2): 20*log10(0.3536) = -9.03 dBFS.
    assert abs(a.rms_dbfs(sine_pcm(440, 16000, 0.1, amplitude=0.5)) - (-9.03)) < 0.1


def test_encode_wav_round_trip():
    pcm = sine_pcm(440, 16000, 0.2)
    fmt, decoded = a.decode_wav(a.encode_wav(pcm, a.l16(16000)))
    assert fmt == a.l16(16000) and decoded == pcm
