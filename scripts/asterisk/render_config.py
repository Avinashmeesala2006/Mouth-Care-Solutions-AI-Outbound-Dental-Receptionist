"""Render the Asterisk configuration from the project settings into a git-ignored folder.

    .venv\\Scripts\\python.exe scripts/asterisk/render_config.py [--out telephony/asterisk/generated]

The output contains the AMI secret (and SIP password when configured); it is written
only to disk, never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.core.config import settings  # noqa: E402
from backend.app.telephony.asterisk_config import write  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'telephony' / 'asterisk' / 'generated'))
    args = parser.parse_args()
    if not settings.asterisk_ami_secret:
        print('ASTERISK_AMI_SECRET is not set; run scripts/setup_asterisk.ps1 (it generates one into .env).')
        return 2
    for path in write(settings, Path(args.out)):
        print(f'rendered {path.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
