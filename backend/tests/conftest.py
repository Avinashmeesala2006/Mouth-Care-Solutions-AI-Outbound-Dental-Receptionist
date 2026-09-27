"""Shared fixtures. Tests never read the project .env and never place real calls."""
import json
import math
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.app import main  # noqa: E402
from backend.app.db.memory import InMemoryCallRepository  # noqa: E402
from backend.app.runtime import Runtime  # noqa: E402

SIGNATURE_SECRET = 'test-signature-secret-0123456789abcdef'
FROM_NUMBER = '+12025550100'          # fictional (555-01xx is reserved for fiction)
PATIENT = '+12025550143'
OTHER = '+12025550177'
PUBLIC = 'https://voice.example.com'


class FakeTwilioClient:
    """Behaves like a Twilio account: type, owned numbers, verified caller IDs, geo permission."""

    def __init__(self, error=None, *, account_type='Full', status='active', owned=(FROM_NUMBER,), voice=True,
                 verified=(), country_permission=True, authenticated=True):
        self.error = error
        self.account_type, self.status, self.authenticated = account_type, status, authenticated
        self.owned, self.voice, self.verified = list(owned), voice, list(verified)
        self.country_permission = country_permission
        self.created = []
        self.hangups = []

    async def account(self):
        if not self.authenticated:
            return {'authenticated': False, 'http_status': 401}
        return {'authenticated': True, 'status': self.status, 'type': self.account_type}

    async def incoming_numbers(self):
        return [{'phone_number': n, 'capabilities': {'voice': self.voice, 'sms': True}} for n in self.owned]

    async def outgoing_caller_ids(self):
        return [{'phone_number': n} for n in self.verified]

    async def dialing_permission(self, iso_code):
        return self.country_permission

    async def create_call(self, **kwargs):
        if self.error:
            raise self.error
        self.created.append(kwargs)
        from backend.app.telephony.twilio import TwilioCall
        return TwilioCall(sid=f'CA{len(self.created):032d}', status='queued')

    async def hangup(self, call_sid):
        self.hangups.append(call_sid)
        return True


LIVE_SETTINGS = dict(app_mode='development', twilio_enabled=True, twilio_account_sid='AC' + '1' * 32,
                     twilio_auth_token='t' * 32, twilio_from_number=FROM_NUMBER, public_base_url=PUBLIC,
                     outbound_allowed_destinations='', outbound_rate_limit_seconds=300, max_concurrent_app_calls=5,
                     service_quota_seconds=None, service_quota_hours=None, twilio_max_call_seconds=900,
                     fish_speech_live_synthesis=False, database_url='', jwt_secret='x' * 48)


@pytest.fixture
def make_runtime(monkeypatch):
    """Build a runtime on the shared settings object with in-memory state and fake Twilio."""
    def build(*, live=True, client=None, repo=None, **overrides):
        values = {**(LIVE_SETTINGS if live else {'twilio_enabled': False}), **overrides}
        for key, value in values.items():
            monkeypatch.setattr(main.settings, key, value)
        rt = Runtime(main.settings, repo=repo or InMemoryCallRepository())
        rt.telephony.client = client or FakeTwilioClient()
        monkeypatch.setattr(main, 'runtime', rt)
        monkeypatch.setattr(main.app.state, 'runtime', rt)
        return rt
    return build


@pytest.fixture
def pg_url():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is not set (PostgreSQL contract tests)')
    import psycopg

    from backend.app.db.migrations import migrate
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute('DROP SCHEMA public CASCADE')
        conn.execute('CREATE SCHEMA public')
    migrate(url)
    return url


def speechlike_pcm(seconds=1.0, rate=16000, level=9000, seed=7) -> bytes:
    """Loud, modulated noise that energy VAD treats as speech (16-bit LE mono)."""
    rng = random.Random(seed)
    out = bytearray()
    for i in range(int(seconds * rate)):
        t = i / rate
        envelope = 0.55 + 0.45 * abs(math.sin(2 * math.pi * 3.0 * t))
        value = int(level * envelope * (0.6 * math.sin(2 * math.pi * 180 * t) + 0.4 * rng.uniform(-1, 1)))
        out += max(-32768, min(32767, value)).to_bytes(2, 'little', signed=True)
    return bytes(out)


def silence_pcm(seconds=1.0, rate=16000) -> bytes:
    return b'\x00\x00' * int(seconds * rate)


def frames(pcm: bytes, frame_bytes: int = 640):
    return [pcm[i:i + frame_bytes] for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]


def json_bytes(data) -> bytes:
    return json.dumps(data).encode()
