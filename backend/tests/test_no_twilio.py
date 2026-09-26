"""Regression guard: the active runtime has no Twilio code, configuration, routes or dependency."""
import re
import subprocess
import sys
from pathlib import Path

from backend.app import main
from backend.app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r'twilio|twiml|voice_text_to_speech|voice_play_audio|voice_auto_response|<Say[\s>]', re.IGNORECASE)
RUNTIME_PATHS = ['backend/app', 'frontend/src', 'scripts', 'knowledge', 'backend/requirements.txt', '.env.example',
                 'docker-compose.yml', 'Dockerfile', 'frontend/index.html', 'frontend/vite.config.js', 'frontend/package.json']


def runtime_files():
    for rel in RUNTIME_PATHS:
        path = ROOT / rel
        files = [path] if path.is_file() else [p for p in path.rglob('*') if p.is_file()]
        for file in files:
            is_test = file.name.startswith('test_') or '.test.' in file.name
            if '__pycache__' not in file.parts and file.suffix not in {'.pyc', '.wav'} and not is_test:
                yield file


def test_no_twilio_references_in_active_runtime_files():
    offenders = [str(f.relative_to(ROOT)) for f in runtime_files() if FORBIDDEN.search(f.read_text(encoding='utf-8', errors='ignore'))]
    assert offenders == []


def test_no_twilio_routes_or_settings():
    assert not [r.path for r in main.app.routes if 'twilio' in getattr(r, 'path', '').lower()]
    assert not [name for name in Settings.model_fields if 'twilio' in name]


def test_backend_imports_without_the_twilio_package():
    code = 'import sys; import backend.app.main; print(any(m.split(".")[0] == "twilio" for m in sys.modules))'
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                            env={'CALL_STATE_DB_PATH': ':memory:', 'PYTEST_CURRENT_TEST': 'x', 'SYSTEMROOT': 'C:\\Windows', 'PATH': ''})
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip().endswith('False')
