"""Packaged Fish Speech voice pack: loading and truthful validation.

The phone path plays pre-generated Fish Speech audio instead of running CPU TTS
inside a live phone call. An asset is only served when it is listed in the
manifest with a matching hash, is 8 kHz mono 16-bit PCM, was generated from the
configured reference voice (reference SHA-256 match), was ASR-verified by the
generator for the current canonical prompt text, and still measures as speech
(rejects constant tones and silence even if a manifest claims otherwise).
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import math
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path

GENERATION_STATUS = 'fish_http_reference_conditioned'
VALIDATION_STATUS = 'asr_verified'
MIN_ENERGY_MODULATION = 0.3
MIN_ACTIVE_FRACTION = 0.35


@dataclass(frozen=True)
class VoiceAsset:
    asset_id: str
    content: bytes
    sha256: str
    duration_seconds: float
    text: str


@dataclass
class VoicePackStatus:
    pack_dir: Path
    required: list[str]
    manifest_present: bool = False
    reference_matches: bool = False
    assets: dict[str, VoiceAsset] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    manifest_version: str | None = None

    @property
    def complete(self) -> bool:
        return self.manifest_present and not any(e.startswith(('missing:', 'manifest_entry_missing:')) for e in self.errors)

    @property
    def valid(self) -> bool:
        return self.manifest_present and self.reference_matches and not self.errors and set(self.assets) == set(self.required)

    def summary(self) -> dict:
        return {
            'ready': self.valid,
            'complete': self.complete,
            'valid': self.valid,
            'manifest': self.manifest_present,
            'reference_matches': self.reference_matches,
            'manifest_version': self.manifest_version,
            'required_assets': len(self.required),
            'verified_assets': len(self.assets),
            'errors': self.errors,
        }


def _words(text: str) -> int:
    return len(re.findall(r"[a-z0-9']+", text.lower().replace('-', ' ')))


def speech_metrics(content: bytes) -> dict:
    """Return format and speech-likeness metrics for a WAV file (stdlib only)."""
    with wave.open(io.BytesIO(content), 'rb') as wav_file:
        rate, channels, width, frames = (wav_file.getframerate(), wav_file.getnchannels(),
                                         wav_file.getsampwidth(), wav_file.getnframes())
        pcm = wav_file.readframes(frames)
    metrics = {'sample_rate': rate, 'channels': channels, 'sample_width': width, 'frames': frames,
               'duration_seconds': frames / rate if rate else 0.0, 'active_fraction': 0.0, 'energy_modulation': 0.0}
    if width != 2 or channels != 1 or rate <= 0 or frames <= 0:
        return metrics
    samples = array.array('h')
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    window = max(1, int(rate * 0.02))
    threshold = 32768 * 10 ** (-40 / 20)
    levels = []
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start:start + window]
        levels.append(math.sqrt(sum(s * s for s in chunk) / window))
    active = [level for level in levels if level > threshold]
    if levels:
        metrics['active_fraction'] = len(active) / len(levels)
    if active:
        mean = sum(active) / len(active)
        metrics['energy_modulation'] = math.sqrt(sum((a - mean) ** 2 for a in active) / len(active)) / mean
    return metrics


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_NUMBER_WORDS = {'zero', 'oh', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'twenty',
                 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety', 'hundred', 'thousand', 'plus'}


def transcript_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or '').lower().replace('-', ' '))


def _contains_sequence(words: list[str], phrase: list[str]) -> bool:
    return any(words[i:i + len(phrase)] == phrase for i in range(len(words) - len(phrase) + 1))


def transcript_problems(transcript: str, prompt: dict) -> list[str]:
    """Content checks an ASR transcript must pass for an approved utterance.

    Shared by the voice-pack generator and the runtime validator so both apply the
    same rules: keywords, exact required phrases (with listed alternatives) for
    fact-bearing wording such as emergency guidance, and no stuttered or repeated
    words that the approved text does not contain.
    """
    heard = transcript_tokens(transcript)
    expected = transcript_tokens(prompt['text'])
    problems = []
    missing = [k for k in prompt.get('keywords', []) if not any(k in word for word in heard)]
    if missing:
        problems.append('asr_keywords_missing:' + ','.join(missing))
    for requirement in prompt.get('required_phrases', []):
        options = requirement if isinstance(requirement, list) else [requirement]
        if not any(_contains_sequence(heard, transcript_tokens(option)) for option in options):
            problems.append('asr_required_phrase_missing:' + options[0])
    for i in range(len(heard) - 1):
        word = heard[i]
        if word == heard[i + 1] and word not in _NUMBER_WORDS and not word.isdigit() and not _contains_sequence(expected, [word, word]):
            problems.append(f'asr_repeated_word:{word}')
            break
    for i in range(len(heard) - 3):
        bigram = heard[i:i + 2]
        if bigram == heard[i + 2:i + 4] and not _contains_sequence(expected, bigram + bigram):
            problems.append('asr_repeated_phrase:' + ' '.join(bigram))
            break
    return problems


def load_voice_pack(pack_dir: Path, prompts: dict, reference_path: Path) -> VoicePackStatus:
    status = VoicePackStatus(pack_dir=pack_dir, required=list(prompts))
    manifest_path = pack_dir / 'manifest.json'
    if not manifest_path.is_file():
        status.errors.append('manifest_missing')
        return status
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        entries = {entry['asset_id']: entry for entry in manifest['assets']}
    except (OSError, ValueError, KeyError, TypeError):
        status.errors.append('manifest_invalid')
        return status
    status.manifest_present = True
    status.manifest_version = manifest.get('pack_version')
    if not reference_path.is_file():
        status.errors.append('reference_audio_missing')
    elif manifest.get('reference_audio_sha256') != sha256_file(reference_path):
        status.errors.append('reference_audio_mismatch')
    else:
        status.reference_matches = True

    for asset_id, prompt in prompts.items():
        entry = entries.get(asset_id)
        path = pack_dir / f'{asset_id}.wav'
        if not path.is_file():
            status.errors.append(f'missing:{asset_id}')
            continue
        if not entry:
            status.errors.append(f'manifest_entry_missing:{asset_id}')
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        problems = []
        if digest != entry.get('sha256'):
            problems.append('hash')
        if entry.get('generation_status') != GENERATION_STATUS:
            problems.append('not_fish_reference_generated')
        if entry.get('validation_status') != VALIDATION_STATUS:
            problems.append('not_asr_verified')
        if entry.get('source_text') != prompt['text']:
            problems.append('text_changed')
        transcript = (entry.get('asr') or {}).get('transcript')
        if not isinstance(transcript, str):
            problems.append('asr_transcript_missing')
        else:
            problems.extend(transcript_problems(transcript, prompt))
        try:
            metrics = speech_metrics(content)
        except (wave.Error, EOFError, ValueError):
            problems.append('invalid_wav')
            metrics = None
        if metrics:
            if (metrics['sample_rate'], metrics['channels'], metrics['sample_width']) != (8000, 1, 2) or metrics['frames'] <= 0:
                problems.append('format')
            elif (metrics['energy_modulation'] < MIN_ENERGY_MODULATION
                  or metrics['active_fraction'] < MIN_ACTIVE_FRACTION
                  or metrics['duration_seconds'] < 0.2 * _words(prompt['text']) + 0.4):
                problems.append('not_speech')
        if problems:
            status.errors.extend(f'{problem}:{asset_id}' for problem in problems)
            continue
        if not status.reference_matches:
            continue  # audio is not provably from the configured reference voice
        status.assets[asset_id] = VoiceAsset(asset_id, content, digest, round(metrics['duration_seconds'], 3), prompt['text'])
    for extra in sorted(set(entries) - set(prompts)):
        status.errors.append(f'unexpected_manifest_entry:{extra}')
    return status


def load_prompts(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))['prompts']
