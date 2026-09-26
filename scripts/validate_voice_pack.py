"""Validate the packaged voice pack with the same rules the backend enforces at runtime.

    python scripts/validate_voice_pack.py            # exit 0 only when every asset is verified
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.config import VOICE_PROMPTS_PATH, settings  # noqa: E402
from backend.app.services.voice_pack import load_prompts, load_voice_pack, speech_metrics  # noqa: E402


def main() -> int:
    prompts = load_prompts(VOICE_PROMPTS_PATH)
    pack_dir = settings.project_path(settings.fish_speech_voice_pack_dir)
    status = load_voice_pack(pack_dir, prompts, settings.project_path(settings.fish_speech_reference_audio))
    report = status.summary()
    report['assets'] = {}
    for asset_id in prompts:
        path = pack_dir / f'{asset_id}.wav'
        if path.is_file():
            metrics = speech_metrics(path.read_bytes())
            report['assets'][asset_id] = {
                'verified': asset_id in status.assets,
                'duration_seconds': round(metrics['duration_seconds'], 2),
                'energy_modulation': round(metrics['energy_modulation'], 3),
                'active_fraction': round(metrics['active_fraction'], 3),
            }
    print(json.dumps(report, indent=2))
    return 0 if status.valid else 1


if __name__ == '__main__':
    sys.exit(main())
