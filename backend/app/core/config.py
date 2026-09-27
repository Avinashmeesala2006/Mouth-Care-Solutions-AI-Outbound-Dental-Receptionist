"""Application settings with one authoritative, explainable effective value per key.

Precedence is the standard pydantic-settings order: process environment, then the
project-root .env file, then the defaults below. ``Settings.resolve()`` turns the raw
values into the effective runtime configuration and reports contradictions as errors
instead of silently rewriting them.

Environments (``APP_MODE``):

* ``development`` - local work. Twilio may stay disabled, call state may live in memory
  when ``DATABASE_URL`` is unset (clearly reported), demo booking slots are available.
* ``staging`` / ``production`` - fail closed: Twilio, PostgreSQL, Fish Speech, signed
  webhooks and a strong ``JWT_SECRET`` are mandatory, and critical values must be set
  explicitly (a silently applied default such as a loopback URL is an error).

Telephony is the Twilio Voice API; speech is served by Fish Speech (packaged,
pre-generated voice assets plus optional live synthesis).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / '.env'
APPROVED_KNOWLEDGE_PATH = PROJECT_ROOT / 'knowledge' / 'clinic' / 'approved.json'
VOICE_PROMPTS_PATH = PROJECT_ROOT / 'knowledge' / 'clinic' / 'voice_prompts.json'
MIGRATIONS_DIR = PROJECT_ROOT / 'migrations'
APPROVED_FACTS = json.loads(APPROVED_KNOWLEDGE_PATH.read_text(encoding='utf-8'))['facts']
APPROVED_CLINIC_PHONE = APPROVED_FACTS['phone']
E164 = re.compile(r'^\+[1-9]\d{7,14}$')
APP_MODES = ('development', 'staging', 'production')
LEGACY_APP_MODES = {'demo': 'development', 'live': 'production'}
WEAK_SECRETS = {'', 'change-me-in-live-mode', 'replace-in-live-mode', 'changeme', 'secret', 'change-me'}


def _should_ignore_env_file() -> bool:
    return 'PYTEST_CURRENT_TEST' in os.environ or any('pytest' in arg.lower() for arg in sys.argv)


def _digits(value: str) -> str:
    return ''.join(ch for ch in value or '' if ch.isdigit())


def mask_phone(value: str | None) -> str:
    value = (value or '').strip()
    if len(value) <= 6:
        return '***' if value else ''
    return value[:3] + '*' * (len(value) - 7) + value[-4:]


def mask_id(value: str | None) -> str:
    """Identifier safe for logs and reports: first 8 characters only."""
    value = (value or '').strip()
    return value[:8] + '...' if len(value) > 8 else value


def _is_private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified
    except ValueError:
        return host in {'localhost'} or host.endswith(('.local', '.localhost', '.internal'))


def parse_public_origin(value: str, name: str) -> tuple[str, str | None]:
    """Return (origin, error). The value must be a bare https origin on a public host."""
    candidate = (value or '').strip()
    if not candidate:
        return '', None
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return '', f'{name} is not a valid URL'
    if parsed.scheme.lower() != 'https':
        return '', f'{name} must use https (got {parsed.scheme or "no scheme"})'
    if parsed.username or parsed.password:
        return '', f'{name} must not contain credentials'
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host or _is_private_host(host):
        return '', f'{name} must be a public hostname (got {host or "none"})'
    if parsed.path not in {'', '/'} or parsed.params or parsed.query or parsed.fragment:
        return '', (f'{name} must be the public origin only; remove the path {parsed.path!r}. '
                    'UI routes such as /demo are separate from API routes.')
    netloc = host if port in (None, 443) else f'{host}:{port}'
    return f'https://{netloc}', None


def normalize_e164(value: str | None, default_country_code: str = '91') -> str | None:
    """Normalize a phone number to E.164 (+CC...). Returns None when it cannot be valid.

    Accepts '+919876543210', '919876543210', '09876543210' and national 10-digit numbers
    (prefixed with ``default_country_code``). Non-ASCII digits are rejected.
    """
    raw = (value or '').strip()
    if not raw or any(ch.isdigit() and not ch.isascii() for ch in raw):
        return None
    if re.search(r'[^\d\s()+.-]', raw):
        return None
    digits = _digits(raw)
    cc = _digits(default_country_code)
    if raw.startswith('+'):
        candidate = '+' + digits
    elif raw.startswith('00'):
        candidate = '+' + digits[2:]
    elif len(digits) == 10 and cc:
        candidate = '+' + cc + digits
    elif len(digits) == 11 and digits.startswith('0') and cc:
        candidate = '+' + cc + digits[1:]
    elif cc and digits.startswith(cc) and len(digits) == len(cc) + 10:
        candidate = '+' + digits
    elif 8 <= len(digits) <= 15 and not digits.startswith('0'):
        candidate = '+' + digits
    else:
        return None
    return candidate if E164.match(candidate) else None


@dataclass
class ResolvedConfig:
    app_mode: str
    production_like: bool
    twilio_enabled: bool
    public_origin: str
    outbound_url: str
    gather_url: str
    status_callback_url: str
    from_number: str
    allowed_destinations: list[str]
    quota_seconds: int
    signature_verification: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        """True when real telephony is enabled (real calls can be placed)."""
        return self.twilio_enabled

    @property
    def call_provider(self) -> str:
        return 'twilio' if self.twilio_enabled else 'disabled'


class Settings(BaseSettings):
    app_name: str = 'Mouth Care Solutions AI Receptionist'
    app_mode: str = 'development'
    clinic_email: str = APPROVED_FACTS['email']
    clinic_phone: str = APPROVED_CLINIC_PHONE
    admin_demo_token: str = 'demo-admin-token'   # accepted only in development
    admin_initial_email: str = ''
    admin_initial_password: str = ''
    jwt_secret: str = 'change-me-in-live-mode'
    public_base_url: str = Field(default='', validation_alias=AliasChoices('PUBLIC_BASE_URL', 'TWILIO_WEBHOOK_BASE_URL'))
    notification_provider: str = 'mock'
    default_country_code: str = '91'
    log_level: str = 'INFO'

    # Existing AI seam (the phone path uses the grounded receptionist engine) -------------
    llm_provider: str = 'openai-compatible'
    llm_api_key: str = ''
    openai_api_key: str = ''
    openai_api_base: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4o-mini'
    frontend_api_base_url: str = 'http://localhost:8000'
    vite_api_base_url: str = ''

    # PostgreSQL --------------------------------------------------------------------------
    database_url: str = ''
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    database_auto_migrate: bool = False

    # Telephony: Twilio Voice API ------------------------------------------------------------
    twilio_enabled: bool = False
    twilio_account_sid: str = ''
    twilio_auth_token: str = ''
    twilio_from_number: str = Field(default='', validation_alias=AliasChoices(
        'TWILIO_FROM_NUMBER', 'OUTBOUND_FROM_NUMBER', 'TWILIO_PHONE_NUMBER'))
    twilio_validate_signature: bool = True
    twilio_http_timeout_seconds: float = 15.0
    twilio_max_call_seconds: int = 900
    twilio_test_to: str = ''

    # Outbound policy, quota and capacity ------------------------------------------------------
    outbound_allowed_destinations: str = ''   # optional allow-list (E.164, comma separated)
    outbound_rate_limit_seconds: int = 300
    service_quota_hours: float | None = None
    service_quota_seconds: int | None = None
    max_concurrent_app_calls: int = 5

    # Conversation ---------------------------------------------------------------------------
    max_call_turns: int = 24

    # Fish Speech (the only TTS engine) ---------------------------------------------------------
    fish_speech_enabled: bool = True
    fish_speech_base_url: str = 'http://127.0.0.1:8080'
    fish_speech_api_key: str = ''
    fish_speech_model: str = 'fish-speech-1.5'
    fish_speech_version: str = Field(
        default='v1.5.1', validation_alias=AliasChoices('FISH_SPEECH_VERSION', 'FISH_SPEECH_GIT_REF'))
    fish_speech_reference_audio: str = 'fish-references/mouth-care-receptionist-reference-20260919.wav'
    fish_speech_reference_text: str = ('Tomorrow is holiday because of Sunday. The Sunday is because of today is Saturday. '
                                       'Today is Saturday is because of yesterday is Friday. Friday is because of Thursday. '
                                       'But I know you are not willing to listen, but you have to listen.')
    fish_speech_reference_id: str = ''
    fish_speech_sample_rate: int | None = None   # expected decoder rate; the WAV header is authoritative
    fish_speech_channels: int = 1
    fish_speech_timeout_seconds: float = 900.0
    fish_speech_max_new_tokens: int = 1024
    fish_speech_chunk_length: int = 200
    fish_speech_top_p: float = 0.7
    fish_speech_repetition_penalty: float = 1.2
    fish_speech_temperature: float = 0.7
    fish_speech_seed: int | None = 20260926
    fish_speech_streaming: bool = False
    fish_speech_live_synthesis: bool = False
    fish_speech_live_first_audio_budget_seconds: float = 4.0
    fish_speech_max_concurrent_requests: int = 1
    fish_speech_voice_pack_dir: str = 'artifacts/voice-pack'
    fish_speech_cache_dir: str = 'artifacts/tts-cache'

    model_config = SettingsConfigDict(
        env_file=None if _should_ignore_env_file() else ENV_FILE,
        env_file_encoding='utf-8',
        env_ignore_empty=True,
        extra='ignore',
        populate_by_name=True,
    )

    def project_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def mode(self) -> str:
        mode = (self.app_mode or '').strip().lower()
        return LEGACY_APP_MODES.get(mode, mode)

    @property
    def is_development(self) -> bool:
        return self.mode == 'development'

    @property
    def mock_mode(self) -> bool:
        """Compatibility view for the web demo: no real telephony is enabled."""
        return not self.twilio_enabled

    def quota_seconds(self) -> int:
        if self.service_quota_seconds is not None:
            return int(self.service_quota_seconds)
        if self.service_quota_hours is not None:
            return int(round(self.service_quota_hours * 3600))
        return 50 * 3600

    def resolve(self) -> ResolvedConfig:
        errors: list[str] = []
        warnings: list[str] = []
        raw_mode = (self.app_mode or '').strip().lower()
        mode = self.mode
        if raw_mode in LEGACY_APP_MODES:
            warnings.append(f'APP_MODE={raw_mode} is a legacy value; use APP_MODE={mode}')
        if mode not in APP_MODES:
            errors.append(f'APP_MODE must be one of {", ".join(APP_MODES)} (got {raw_mode or "unset"})')
        production_like = mode in {'staging', 'production'}
        # Public URLs --------------------------------------------------------------------------
        public_origin, public_error = parse_public_origin(self.public_base_url, 'PUBLIC_BASE_URL')
        if public_error:
            errors.append(public_error)
        outbound_url = f'{public_origin}/api/telephony/twilio/outbound' if public_origin else ''
        gather_url = f'{public_origin}/api/telephony/twilio/gather' if public_origin else ''
        status_callback_url = f'{public_origin}/api/telephony/twilio/status' if public_origin else ''

        # Twilio --------------------------------------------------------------------------------
        from_number = normalize_e164(self.twilio_from_number, self.default_country_code) or ''
        if self.twilio_from_number and not from_number:
            errors.append('TWILIO_FROM_NUMBER must be a valid E.164 number')
        if self.twilio_test_to and not normalize_e164(self.twilio_test_to, self.default_country_code):
            errors.append('TWILIO_TEST_TO must be a valid phone number')
        signature_verification = bool(self.twilio_auth_token and self.twilio_validate_signature)
        if production_like and not self.twilio_enabled:
            errors.append(f'APP_MODE={mode} requires TWILIO_ENABLED=true')
        if self.twilio_enabled:
            missing = [name for name, value in (('TWILIO_ACCOUNT_SID', self.twilio_account_sid),
                                                ('TWILIO_AUTH_TOKEN', self.twilio_auth_token),
                                                ('TWILIO_FROM_NUMBER', self.twilio_from_number),
                                                ('PUBLIC_BASE_URL', self.public_base_url)) if not value]
            if missing:
                errors.append('TWILIO_ENABLED=true requires ' + ', '.join(missing))
            if not signature_verification:
                message = 'TWILIO signature validation is not configured'
                (errors if production_like else warnings).append(message)

        # PostgreSQL ------------------------------------------------------------------------------
        if not self.database_url:
            if production_like:
                errors.append(f'APP_MODE={mode} requires DATABASE_URL (PostgreSQL)')
            else:
                warnings.append('DATABASE_URL is not set: call state is kept in memory (development only, lost on restart)')
        elif not self.database_url.startswith(('postgresql://', 'postgres://')):
            errors.append('DATABASE_URL must be a PostgreSQL URL (postgresql://...)')
        if self.database_pool_max_size < max(1, self.database_pool_min_size):
            errors.append('DATABASE_POOL_MAX_SIZE must be >= DATABASE_POOL_MIN_SIZE')

        # Fish Speech -------------------------------------------------------------------------------
        if not self.fish_speech_enabled:
            (errors if production_like or self.twilio_enabled else warnings).append(
                'FISH_SPEECH_ENABLED=false: Fish Speech is the required TTS engine for calls')
        if self.fish_speech_channels != 1:
            errors.append('FISH_SPEECH_CHANNELS must be 1 (mono telephone audio)')
        if not self.project_path(self.fish_speech_reference_audio).is_file():
            errors.append('FISH_SPEECH_REFERENCE_AUDIO does not exist')
        if not self.fish_speech_reference_text.strip() and not self.fish_speech_reference_id:
            errors.append('FISH_SPEECH_REFERENCE_TEXT is required with reference audio')
        if not 100 <= self.fish_speech_chunk_length <= 300:
            errors.append('FISH_SPEECH_CHUNK_LENGTH must be between 100 and 300 (Fish Speech 1.5 API limit)')

        # Quota and capacity ----------------------------------------------------------------------------
        if self.service_quota_seconds is not None and self.service_quota_hours is not None:
            if int(round(self.service_quota_hours * 3600)) != int(self.service_quota_seconds):
                errors.append('SERVICE_QUOTA_HOURS and SERVICE_QUOTA_SECONDS disagree '
                              f'({self.service_quota_hours} h != {self.service_quota_seconds} s)')
        quota = self.quota_seconds()
        if quota <= 0:
            errors.append('service quota must be positive')
        if self.max_concurrent_app_calls < 1:
            errors.append('MAX_CONCURRENT_APP_CALLS must be at least 1')
        allowed = []
        for item in (d.strip() for d in self.outbound_allowed_destinations.split(',')):
            if not item:
                continue
            normalized = normalize_e164(item, self.default_country_code)
            if normalized and item.startswith('+'):
                allowed.append(normalized)
            else:
                errors.append('OUTBOUND_ALLOWED_DESTINATIONS entries must be E.164 numbers (+CC...)')

        # Security -----------------------------------------------------------------------------------------
        weak_jwt = self.jwt_secret in WEAK_SECRETS or len(self.jwt_secret) < 32
        if weak_jwt:
            (errors if production_like else warnings).append(
                'JWT_SECRET is weak or a published placeholder (use 32+ random characters)')
            warnings.append('ADMIN_INITIAL_EMAIL/ADMIN_INITIAL_PASSWORD unset; admin API (including outbound calls) is disabled')
        if _digits(self.clinic_phone) != _digits(APPROVED_CLINIC_PHONE):
            warnings.append('CLINIC_PHONE differs from approved clinic knowledge and is ignored')
        if production_like:
            warnings.append('live appointment scheduling is not connected: phone appointments are recorded as requests '
                            'for the clinic team to confirm')
        return ResolvedConfig(
            app_mode=mode, production_like=production_like, twilio_enabled=self.twilio_enabled,
            public_origin=public_origin, outbound_url=outbound_url, gather_url=gather_url,
            status_callback_url=status_callback_url, from_number=from_number,
            allowed_destinations=allowed, quota_seconds=quota, signature_verification=signature_verification,
            errors=errors, warnings=warnings,
        )

    def validate_live(self) -> list[str]:
        """Blocking configuration errors (empty when the configuration is consistent)."""
        return self.resolve().errors

    def _env_file(self) -> Path | None:
        env_file = self.model_config.get('env_file')
        return Path(env_file) if isinstance(env_file, (str, Path)) and Path(env_file).is_file() else None

    def value_sources(self) -> dict[str, str]:
        """Where each effective value came from: process env, the .env file, or the default."""
        file_keys = set()
        env_file = self._env_file()
        if env_file:
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    if value.strip():
                        file_keys.add(key.strip().upper())
        sources = {}
        for name, info in type(self).model_fields.items():
            aliases = [name.upper()]
            if isinstance(info.validation_alias, AliasChoices):
                aliases = [str(a).upper() for a in info.validation_alias.choices]
            if any(os.environ.get(a, '').strip() for a in aliases):
                sources[name] = 'process_env'
            elif any(a in file_keys for a in aliases):
                sources[name] = '.env'
            elif name in self.model_fields_set:
                sources[name] = 'explicit'
            else:
                sources[name] = 'default'
        return sources

    def env_file_diagnostics(self) -> dict:
        """Duplicate, blank and unknown keys in the .env file (names only, never values)."""
        env_file = self._env_file()
        result: dict[str, list[str]] = {'duplicates': [], 'blank': [], 'unknown': []}
        if env_file is None:
            return result
        seen = set()
        known = set()
        for name, info in type(self).model_fields.items():
            known.add(name.upper())
            if isinstance(info.validation_alias, AliasChoices):
                known.update(str(a).upper() for a in info.validation_alias.choices)
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = (part.strip() for part in line.split('=', 1))
            key = key.upper()
            if key in seen:
                result['duplicates'].append(key)
            seen.add(key)
            if not value:
                result['blank'].append(key)
            if key not in known:
                result['unknown'].append(key)
        return result


settings = Settings()
