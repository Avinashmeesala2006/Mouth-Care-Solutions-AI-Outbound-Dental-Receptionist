#!/usr/bin/env python3
"""Static release gate: stale data, required files/routes, and committed-secret checks.

Local secret files (.env, .env.*, excluding .env.example) are git/docker-ignored and are
not scanned for secrets; every file that can be committed, including .env.example, is.
"""
from pathlib import Path
import re, sys

root = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.venv', '.venv-1', 'node_modules', '__pycache__', '.pytest_cache', 'dist', 'data', 'runtime', 'voice-pack-staging', '.git'}
TEXT_SUFFIXES = {'.py', '.md', '.json', '.yml', '.yaml', '.jsx', '.js', '.css', '.sql', '.ps1', '.sh', '.txt', '.html', '.conf', '.toml', '.ini', '.example'}
fail = []


def local_secret_file(p: Path) -> bool:
    return p.name == '.env' or (p.name.startswith('.env.') and p.name != '.env.example')


files = [p for p in root.rglob('*') if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)
         and p.name != 'zero_error_scan.py' and not local_secret_file(p)]
texts = {p: p.read_text(errors='ignore') for p in files if p.suffix in TEXT_SUFFIXES or p.name in {'.env.example', 'Dockerfile', 'Makefile'}}
source = (root / 'backend/app/main.py').read_text()
frontend_text = '\n'.join(t for p, t in texts.items() if 'frontend' in p.parts)

for value, label in [('9866344866', 'retired clinic phone'), ('98663 44866', 'retired formatted phone'), ('demo-signature', 'legacy mock signature')]:
    if any(value in t for t in texts.values()):
        fail.append(f'{label}: {value}')
if 'http://localhost:8000' in frontend_text.replace("'http://127.0.0.1:8000'", ''):
    fail.append('hard-coded frontend backend URL')
for rel in ['README.md', '.env.example', '.gitignore', '.dockerignore', 'Dockerfile', 'docker-compose.yml', 'backend/app/main.py',
            'backend/app/telephony/asterisk.py', 'backend/app/telephony/agi.py', 'knowledge/clinic/approved.json',
            'knowledge/clinic/voice_prompts.json', 'scripts/setup_asterisk.ps1', 'scripts/asterisk/install_asterisk.sh',
            'artifacts/voice-pack/manifest.json', 'frontend/package-lock.json']:
    if not (root / rel).exists():
        fail.append(f'missing required file: {rel}')
example = (root / '.env.example').read_text()
for key in ['APP_MODE', 'MOCK_MODE', 'CALL_PROVIDER', 'ASTERISK_AMI_SECRET', 'TELEPHONY_INTERFACE', 'OUTBOUND_CALLER_ID',
            'OUTBOUND_ALLOWED_DESTINATIONS', 'SIP_TRUNK_PASSWORD', 'FISH_SPEECH_ENABLED', 'FISH_SPEECH_REFERENCE_AUDIO', 'JWT_SECRET']:
    if not re.search(r'^' + re.escape(key) + r'=', example, re.M):
        fail.append(f'missing env key in .env.example: {key}')
for key in ['ASTERISK_AMI_SECRET', 'SIP_TRUNK_PASSWORD', 'JWT_SECRET', 'ADMIN_INITIAL_PASSWORD', 'LLM_API_KEY', 'OPENAI_API_KEY']:
    if re.search(r'^' + re.escape(key) + r'=\S', example, re.M):
        fail.append(f'.env.example must not contain a value for {key}')
if re.search(r'^PUBLIC_BASE_URL=\S*/demo', example, re.M):
    fail.append('.env.example public URLs must not contain /demo')
for route in ['/api/agent/session', '/api/slots', '/api/booking/hold', '/api/callback', '/api/calls/request',
              '/api/telephony/preflight', '/api/telephony/audio/', '/health', '/ready']:
    if route not in source:
        fail.append(f'missing route: {route}')
say_emitters = [p.relative_to(root) for p in (root / 'backend' / 'app').rglob('*.py')
                if re.search(r"""['"]<Say[\s>]|\.say\(""", p.read_text(errors='ignore'))]
if say_emitters:
    fail.append(f'backend emits a text-to-speech verb in {say_emitters}; caller speech must come from the packaged Fish voice')
gitignore = (root / '.gitignore').read_text()
for pattern in ['.env', '.env.*', '!.env.example', '.venv-*/', 'data/', 'telephony/asterisk/generated/']:
    if pattern not in gitignore.splitlines():
        fail.append(f'.gitignore missing {pattern}')
secret = re.compile(r'(sk-[A-Za-z0-9_-]{20,}|sk-or-v1-[a-f0-9]{20,}|AC[a-f0-9]{32}|SK[a-f0-9]{32}|AIza[A-Za-z0-9_-]{30,}'
                    r'|(?:AUTH_TOKEN|auth_token)\s*[=:]\s*["\']?[a-f0-9]{32})')
for p, t in texts.items():
    if secret.search(t):
        fail.append(f'secret-looking value: {p.relative_to(root)}')
if fail:
    print('ZERO_ERROR_SCAN_FAIL')
    print('\n'.join('- ' + x for x in fail))
    sys.exit(1)
print('ZERO_ERROR_SCAN_PASS')
print(f'scanned {len(texts)} committable text files; configuration, routes, stale data and secret checks passed')
