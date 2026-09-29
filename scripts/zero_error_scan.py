#!/usr/bin/env python3
"""Static release gate: required files/routes/configuration, stale data, deprecated
providers and committed-secret checks.

Local secret files (.env, .env.*, excluding .env.example) and key files are git/docker-ignored
and are not scanned; every file that can be committed, including .env.example, is.
"""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'dist', 'data',
             'runtime', 'voice-pack-staging', 'tts-cache', '.git'}
TEXT_SUFFIXES = {'.py', '.md', '.json', '.yml', '.yaml', '.jsx', '.js', '.css', '.sql', '.ps1', '.sh', '.txt', '.html', '.conf',
                 '.toml', '.ini', '.example'}
fail = []


def local_secret_file(p: Path) -> bool:
    return p.name == '.env' or (p.name.startswith('.env.') and p.name != '.env.example') or p.suffix in {'.key', '.pem'}


files = [p for p in root.rglob('*') if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)
         and p.name != 'zero_error_scan.py' and not local_secret_file(p)]
texts = {p: p.read_text(errors='ignore') for p in files
         if p.suffix in TEXT_SUFFIXES or p.name in {'.env.example', 'Dockerfile', 'Makefile'}}
source = '\n'.join((root / rel).read_text() for rel in ('backend/app/main.py', 'backend/app/telephony/twilio_routes.py'))
frontend_text = '\n'.join(t for p, t in texts.items() if 'frontend' in p.parts)

for value, label in [('9866344866', 'retired clinic phone'), ('98663 44866', 'retired formatted phone'),
                     ('demo-signature', 'legacy mock signature')]:
    if any(value in t for t in texts.values()):
        fail.append(f'{label}: {value}')
if 'http://localhost:8000' in frontend_text.replace("'http://127.0.0.1:8000'", ''):
    fail.append('hard-coded frontend backend URL')
for rel in ['README.md', '.env.example', '.gitignore', '.dockerignore', 'Dockerfile', 'docker-compose.yml', 'backend/app/main.py',
            'backend/app/telephony/twilio_routes.py', 'backend/app/telephony/twilio.py',
            'backend/app/voice/audio_adapter.py', 'backend/app/voice/fish_speech.py', 'backend/app/db/postgres.py',
            'migrations/002_twilio_voice.sql', 'knowledge/clinic/approved.json', 'knowledge/clinic/voice_prompts.json',
            'artifacts/voice-pack/manifest.json', 'scripts/verify_public_route.py',
            'frontend/package-lock.json']:
    if not (root / rel).exists():
        fail.append(f'missing required file: {rel}')
example = (root / '.env.example').read_text()
for key in ['APP_MODE', 'TWILIO_ENABLED', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER',
            'TWILIO_VALIDATE_SIGNATURE', 'PUBLIC_BASE_URL',
            'FISH_SPEECH_ENABLED', 'FISH_SPEECH_BASE_URL', 'FISH_SPEECH_MODEL', 'FISH_SPEECH_REFERENCE_AUDIO',
            'FISH_SPEECH_REFERENCE_TEXT', 'FISH_SPEECH_SAMPLE_RATE', 'FISH_SPEECH_CHANNELS', 'FISH_SPEECH_TIMEOUT_SECONDS',
            'SERVICE_QUOTA_HOURS', 'SERVICE_QUOTA_SECONDS', 'MAX_CONCURRENT_APP_CALLS', 'DATABASE_URL', 'JWT_SECRET',
            'TWILIO_TEST_TO']:
    if not re.search(r'^' + re.escape(key) + r'=', example, re.M):
        fail.append(f'missing env key in .env.example: {key}')
for key in ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER', 'TWILIO_TEST_TO', 'JWT_SECRET',
            'ADMIN_INITIAL_PASSWORD', 'LLM_API_KEY', 'OPENAI_API_KEY', 'DATABASE_URL',
            'FISH_SPEECH_API_KEY']:
    if re.search(r'^' + re.escape(key) + r'=\S', example, re.M):
        fail.append(f'.env.example must not contain a value for {key}')
if re.search(r'^PUBLIC_BASE_URL=\S*/demo', example, re.M):
    fail.append('.env.example public URLs must not contain /demo')
for route in ['/api/agent/session', '/api/slots', '/api/booking/hold', '/api/callback', '/api/calls/request',
              '/api/twilio/preflight', '/api/telephony/audio/', '/health', '/ready', '/api/voice/fish/health',
              "'/outbound'", "'/gather'", "'/status'"]:
    if route not in source:
        fail.append(f'missing route: {route}')
tts_emitters = [p.relative_to(root) for p in (root / 'backend' / 'app').rglob('*.py')
                if re.search(r"""['"]<Say[\s>]|\.say\(|['"]action['"]\s*:\s*['"]talk['"]|\bTalk\(""",
                             p.read_text(errors='ignore'))]
if tts_emitters:
    fail.append(f'backend emits a provider text-to-speech action in {tts_emitters}; caller speech must be Fish Speech')
deprecated = re.compile(r'vonage|conversation.?relay|asterisk|pjsip|elevenlabs|exotel|plivo', re.IGNORECASE)
legacy_provider_paths = {
    Path('backend/app/core/config.py'),
    Path('migrations/003_asterisk_telephony.sql'),
    Path('migrations/004_twilio_provider_default.sql'),
}
for p, t in texts.items():
    if p.relative_to(root) in legacy_provider_paths:
        continue
    is_test = 'tests' in p.parts or p.name.startswith('test_') or '.test.' in p.name   # tests assert absence
    if not is_test and deprecated.search(t):
        fail.append(f'deprecated provider reference: {p.relative_to(root)}')
gitignore = (root / '.gitignore').read_text()
for pattern in ['.env', '.env.*', '!.env.example', '.venv-*/', 'data/', '*.key', '*.pem', 'private.key']:
    if pattern not in gitignore.splitlines():
        fail.append(f'.gitignore missing {pattern}')
dockerignore = (root / '.dockerignore').read_text().splitlines()
for pattern in ['.env', '.env.*', '*.key', '*.pem']:
    if pattern not in dockerignore:
        fail.append(f'.dockerignore missing {pattern}')
secret = re.compile(r'(sk-[A-Za-z0-9_-]{20,}|sk-or-v1-[a-f0-9]{20,}|AC[a-f0-9]{32}|SK[a-f0-9]{32}|AIza[A-Za-z0-9_-]{30,}'
                    r'|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}'
                    r'|(?:AUTH_TOKEN|auth_token)\s*[=:]\s*["\']?[a-f0-9]{32})')
for p, t in texts.items():
    if secret.search(t):
        fail.append(f'secret-looking value: {p.relative_to(root)}')
if fail:
    print('ZERO_ERROR_SCAN_FAIL')
    print('\n'.join('- ' + x for x in fail))
    sys.exit(1)
print('ZERO_ERROR_SCAN_PASS')
print(f'scanned {len(texts)} committable text files; configuration, routes, providers, stale data and secret checks passed')
