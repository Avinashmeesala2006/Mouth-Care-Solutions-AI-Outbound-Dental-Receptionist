"""Application settings with one authoritative, explainable effective value per key.

Precedence is the standard pydantic-settings order: process environment, then the
project-root .env file, then the defaults below. ``Settings.resolve()`` turns the raw
values into the effective runtime configuration and reports contradictions as errors
instead of silently rewriting them.

Environments (``APP_MODE``):

* ``development`` - local work. Asterisk may stay disabled, call state may live in memory
  when ``DATABASE_URL`` is unset (clearly reported), demo booking slots are available.
* ``staging`` / ``production`` - fail closed: Asterisk, PostgreSQL, Fish Speech, speech
  recognition and a strong ``JWT_SECRET`` are mandatory.

Telephony is Asterisk, controlled over ARI (REST + WebSocket), dialling a GSM/LTE voice
gateway over PJSIP/SIP. Caller-facing speech is Fish Speech (the verified voice pack plus
optional live synthesis); caller speech is recognised locally (faster-whisper).
``LIVE_CALL_ALLOWED`` is the operator's master switch for placing real calls.
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
WEAK_SECRETS = {'', 'change-me-in-live-mode', 'replace-in-live-mode', 'changeme', 'secret', 'change-me',
                'password', 'asterisk', 'admin'}
GATEWAY_MODES = ('register', 'static')
SIP_CODECS = ('alaw', 'ulaw', 'g722', 'gsm', 'slin')
DTMF_MODES = ('rfc4733', 'inband', 'info', 'auto')
STT_PROVIDERS = ('faster-whisper', 'disabled')
_HOST = re.compile(r'^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])$')
_NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_DIAL_PLACEHOLDERS = re.compile(r'\{([^{}]*)\}')


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


def parse_http_origin(value: str, name: str) -> tuple[str, str | None]:
    """Return (origin, error) for an http(s) origin reachable on a private network (no path, no credentials)."""
    candidate = (value or '').strip().rstrip('/')
    if not candidate:
        return '', None
    try:
        parsed = urlparse(candidate)
        parsed.port   # noqa: B018 - validates the port
    except ValueError:
        return '', f'{name} is not a valid URL'
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return '', f'{name} must be an http(s) origin such as http://192.168.1.10:8000'
    if parsed.username or parsed.password:
        return '', f'{name} must not contain credentials'
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        return '', f'{name} must be an origin only (no path)'
    return f'{parsed.scheme}://{parsed.netloc}', None


def _dial_template_error(template: str) -> str | None:
    """ASTERISK_DIAL_TEMPLATE must name one technology/resource with exactly one number placeholder."""
    placeholders = _DIAL_PLACEHOLDERS.findall(template or '')
    numbers = [p for p in placeholders if p in {'e164', 'digits', 'national'}]
    if not re.match(r'^[A-Za-z0-9_]+/[^\s,&]+$', template or '') or len(numbers) != 1 \
            or any(p not in {'e164', 'digits', 'national', 'endpoint'} for p in placeholders):
        return ('ASTERISK_DIAL_TEMPLATE must look like PJSIP/{e164}@{endpoint} (one of {e164}, {digits}, {national}; '
                'no spaces, commas or "&")')
    return None


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
    asterisk_enabled: bool
    live_call_allowed: bool
    ari_base_url: str            # http(s)://host:port/ari - never contains credentials
    ari_events_url: str          # ws(s)://host:port/ari/events?app=... - never contains credentials
    sip_endpoint: str
    caller_id: str
    fastapi_base_url: str
    public_origin: str
    from_number: str
    outbound_url: str
    gather_url: str
    status_callback_url: str
    allowed_destinations: list[str]
    quota_seconds: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gateway_warnings: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        """True when the configured real telephony layer is enabled."""
        return self.call_provider != 'disabled'

    @property
    def call_provider(self) -> str:
        if self.asterisk_enabled:
            return 'asterisk'
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

    # Active telephony: Twilio Voice webhooks and REST API ------------------------------------
    call_provider: str = Field(default='twilio', validation_alias=AliasChoices('CALL_PROVIDER', 'TELEPHONY_PROVIDER'))
    twilio_enabled: bool = False
    twilio_account_sid: str = ''
    twilio_auth_token: str = ''
    twilio_from_number: str = ''
    twilio_validate_signature: bool = True
    twilio_http_timeout_seconds: float = 15.0
    twilio_max_call_seconds: int = 900
    twilio_test_to: str = ''
    public_base_url: str = ''

    # Legacy telephony: Asterisk over ARI (REST + WebSocket) -------------------------------
    asterisk_enabled: bool = False
    asterisk_host: str = '127.0.0.1'           # ARI HTTP server (http.conf); no scheme, no credentials
    asterisk_port: int = 8088
    asterisk_use_tls: bool = False             # https/wss to ARI (http.conf tlsenable)
    asterisk_user: str = Field(default='', validation_alias=AliasChoices('ASTERISK_USER', 'ASTERISK_ARI_USER'))
    asterisk_password: str = Field(default='', validation_alias=AliasChoices('ASTERISK_PASSWORD', 'ASTERISK_ARI_PASSWORD'))
    asterisk_ari_app: str = 'mouthcare'        # Stasis application name
    asterisk_sip_endpoint: str = 'gsm-gateway' # PJSIP endpoint that represents the GSM/LTE gateway
    asterisk_context: str = 'from-gsm-gateway' # dialplan context for calls arriving from the gateway
    asterisk_dial_template: str = 'PJSIP/{e164}@{endpoint}'   # {e164} | {digits} | {national}, {endpoint}
    asterisk_caller_id: str = ''               # optional; a GSM gateway presents the SIM's own number
    asterisk_sounds_prefix: str = 'mouthcare'  # voice pack installed at <sounds>/<language>/<prefix>/<asset>.wav
    asterisk_sound_language: str = 'en'
    asterisk_ring_timeout_seconds: int = 45
    asterisk_max_call_seconds: int = 900
    asterisk_http_timeout_seconds: float = 10.0
    # Rendered Asterisk configuration (scripts/asterisk/render_config.py); FastAPI does not use these at runtime.
    asterisk_http_bind: str = '127.0.0.1'
    asterisk_sip_port: int = 5060
    asterisk_rtp_start: int = 10000
    asterisk_rtp_end: int = 10100
    asterisk_local_net: str = ''               # e.g. 192.168.1.0/24 when Asterisk is behind NAT
    asterisk_external_address: str = ''        # public address advertised in SIP/SDP (only behind NAT)

    # GSM/LTE voice gateway (reached by Asterisk over SIP; FastAPI never talks to it directly) --
    gsm_gateway_mode: str = 'register'         # register: the gateway registers to Asterisk | static: fixed IP peer
    gsm_gateway_host: str = ''
    gsm_gateway_port: int = 5060
    gsm_gateway_sip_user: str = ''
    gsm_gateway_sip_password: str = ''
    gsm_gateway_codecs: str = 'alaw,ulaw'      # G.711 A-law (India/Europe) first, u-law second
    gsm_gateway_dtmf_mode: str = 'rfc4733'
    gsm_gateway_qualify_seconds: int = 30

    # Where Asterisk can fetch dynamically generated Fish audio over HTTP (only with live synthesis).
    fastapi_base_url: str = ''

    # Operator master switch: real calls are placed only when true (and every safety check passes).
    live_call_allowed: bool = False

    # Speech recognition (local; caller audio never leaves the host) ----------------------------
    stt_provider: str = 'faster-whisper'       # faster-whisper | disabled
    stt_model: str = 'base.en'
    stt_language: str = 'en'
    stt_device: str = 'cpu'
    stt_compute_type: str = 'int8'
    stt_beam_size: int = 5
    stt_record_max_seconds: int = 15
    stt_record_max_silence_seconds: int = 3

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
    fish_speech_base_url: str = Field(default='http://127.0.0.1:8080',
                                      validation_alias=AliasChoices('FISH_SPEECH_BASE_URL', 'FISH_SPEECH_URL'))
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
        return not self.asterisk_enabled

    @property
    def ari_base_url(self) -> str:
        return f"{'https' if self.asterisk_use_tls else 'http'}://{self.asterisk_host}:{self.asterisk_port}/ari"

    def dial_endpoint(self, destination_e164: str) -> str:
        """ARI originate endpoint for an E.164 number, e.g. PJSIP/+919876543210@gsm-gateway."""
        digits = _digits(destination_e164)
        cc = _digits(self.default_country_code)
        national = digits[len(cc):] if cc and digits.startswith(cc) else digits
        return self.asterisk_dial_template.format(e164='+' + digits, digits=digits, national=national,
                                                  endpoint=self.asterisk_sip_endpoint)

    def secret_values(self) -> list[str]:
        """Configured secrets, for redaction of diagnostics (never logged or returned)."""
        values = [self.asterisk_password, self.gsm_gateway_sip_password, self.twilio_auth_token, self.jwt_secret,
              self.llm_api_key,
                  self.openai_api_key, self.fish_speech_api_key, self.admin_initial_password]
        try:
            password = urlparse(self.database_url).password if self.database_url else None
        except ValueError:
            password = None
        return [v for v in [*values, password or ''] if v and len(v) >= 4]

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
        provider = (self.call_provider or '').strip().lower()
        if provider not in {'twilio', 'asterisk'}:
            errors.append('CALL_PROVIDER must be one of twilio, asterisk')
            provider = 'twilio'
        twilio_active = provider == 'twilio'
        twilio_required = twilio_active and (self.twilio_enabled or production_like)
        # Asterisk (ARI) ---------------------------------------------------------------------------
        ari_base_url, ari_events_url = '', ''
        if production_like and provider == 'asterisk' and not self.asterisk_enabled:
            errors.append(f'APP_MODE={mode} requires ASTERISK_ENABLED=true')
        if production_like and twilio_active and not self.twilio_enabled:
            errors.append(f'APP_MODE={mode} requires TWILIO_ENABLED=true')
        if self.asterisk_enabled:
            missing = [name for name, value in (('ASTERISK_HOST', self.asterisk_host), ('ASTERISK_USER', self.asterisk_user),
                                                ('ASTERISK_PASSWORD', self.asterisk_password),
                                                ('ASTERISK_ARI_APP', self.asterisk_ari_app),
                                                ('ASTERISK_SIP_ENDPOINT', self.asterisk_sip_endpoint)) if not value]
            if missing:
                errors.append('ASTERISK_ENABLED=true requires ' + ', '.join(missing))
        if self.asterisk_host and not _HOST.match(self.asterisk_host):
            errors.append('ASTERISK_HOST must be a bare host name or IP address (no scheme, path or credentials)')
        if not 1 <= self.asterisk_port <= 65535:
            errors.append('ASTERISK_PORT must be between 1 and 65535')
        for name, value in (('ASTERISK_ARI_APP', self.asterisk_ari_app), ('ASTERISK_SIP_ENDPOINT', self.asterisk_sip_endpoint),
                            ('ASTERISK_CONTEXT', self.asterisk_context), ('ASTERISK_SOUNDS_PREFIX', self.asterisk_sounds_prefix)):
            if value and not _NAME.match(value):
                errors.append(f'{name} may contain only letters, digits, "-" and "_"')
        template_error = _dial_template_error(self.asterisk_dial_template)
        if template_error:
            errors.append(template_error)
        elif not self.asterisk_dial_template.upper().startswith('PJSIP/'):
            warnings.append('ASTERISK_DIAL_TEMPLATE does not use PJSIP; the supported gateway path is PJSIP/SIP')
        caller_id = ''
        if self.asterisk_caller_id:
            caller_id = normalize_e164(self.asterisk_caller_id, self.default_country_code) or ''
            if not caller_id:
                errors.append('ASTERISK_CALLER_ID must be a valid phone number (or empty: the SIM presents its number)')
        if not 10 <= self.asterisk_ring_timeout_seconds <= 120:
            errors.append('ASTERISK_RING_TIMEOUT_SECONDS must be between 10 and 120')
        if not 60 <= self.asterisk_max_call_seconds <= 3600:
            errors.append('ASTERISK_MAX_CALL_SECONDS must be between 60 and 3600')
        if self.asterisk_enabled and self.asterisk_password:
            weak_ari = self.asterisk_password.lower() in WEAK_SECRETS or len(self.asterisk_password) < 16
            if weak_ari:
                (errors if production_like else warnings).append('ASTERISK_PASSWORD is weak (use 16+ random characters)')
            if not self.asterisk_use_tls and not _is_private_host(self.asterisk_host):
                warnings.append('ARI is reached over plain HTTP on a non-private host; enable ASTERISK_USE_TLS or keep '
                                'ARI on loopback/a private network')
        if not errors or self.asterisk_host:
            scheme = 'https' if self.asterisk_use_tls else 'http'
            ari_base_url = f'{scheme}://{self.asterisk_host}:{self.asterisk_port}/ari'
            ari_events_url = (f"{'wss' if self.asterisk_use_tls else 'ws'}://{self.asterisk_host}:{self.asterisk_port}"
                              f'/ari/events?app={self.asterisk_ari_app}&subscribeAll=false')
        if self.live_call_allowed and not self.asterisk_enabled:
            warnings.append('LIVE_CALL_ALLOWED is not the Twilio authorization gate; use /api/twilio/preflight')

        # Twilio Voice -----------------------------------------------------------------------------
        public_origin, public_error = parse_public_origin(self.public_base_url, 'PUBLIC_BASE_URL')
        if public_error:
            (errors if twilio_required else warnings).append(public_error)
        from_number = (normalize_e164(self.twilio_from_number, self.default_country_code)
                   if self.twilio_from_number else '') or ''
        if twilio_required and not self.twilio_account_sid:
            errors.append('TWILIO_ACCOUNT_SID is required for the active Twilio provider')
        if twilio_required and not self.twilio_auth_token:
            errors.append('TWILIO_AUTH_TOKEN is required for the active Twilio provider')
        if twilio_required and not from_number:
            errors.append('TWILIO_FROM_NUMBER must be a valid E.164 number for the active Twilio provider')
        if twilio_required and not public_origin:
            errors.append('PUBLIC_BASE_URL must be a public https origin for the active Twilio provider')
        if twilio_required and not self.twilio_validate_signature:
            errors.append('TWILIO signature validation must remain enabled')
        if not 60 <= self.twilio_max_call_seconds <= 3600:
            errors.append('TWILIO_MAX_CALL_SECONDS must be between 60 and 3600')
        outbound_url = f'{public_origin}/api/telephony/twilio/outbound' if public_origin else ''
        gather_url = f'{public_origin}/api/telephony/twilio/gather' if public_origin else ''
        status_callback_url = f'{public_origin}/api/telephony/twilio/status' if public_origin else ''

        # GSM/LTE gateway (rendered into pjsip.conf by scripts/asterisk/render_config.py) -----------------
        gateway_warnings: list[str] = []
        if self.gsm_gateway_mode not in GATEWAY_MODES:
            errors.append(f'GSM_GATEWAY_MODE must be one of {", ".join(GATEWAY_MODES)}')
        if not 1 <= self.gsm_gateway_port <= 65535:
            errors.append('GSM_GATEWAY_PORT must be between 1 and 65535')
        codecs = [c.strip().lower() for c in self.gsm_gateway_codecs.split(',') if c.strip()]
        if not codecs or any(c not in SIP_CODECS for c in codecs):
            errors.append(f'GSM_GATEWAY_CODECS must be a comma-separated subset of {", ".join(SIP_CODECS)}')
        if self.gsm_gateway_dtmf_mode not in DTMF_MODES:
            errors.append(f'GSM_GATEWAY_DTMF_MODE must be one of {", ".join(DTMF_MODES)}')
        if self.gsm_gateway_host and not _HOST.match(self.gsm_gateway_host):
            errors.append('GSM_GATEWAY_HOST must be a bare host name or IP address')
        if self.gsm_gateway_mode == 'static' and not self.gsm_gateway_host:
            gateway_warnings.append('GSM_GATEWAY_HOST is required for GSM_GATEWAY_MODE=static')
        if self.gsm_gateway_mode == 'register':
            if not self.gsm_gateway_sip_user or not self.gsm_gateway_sip_password:
                gateway_warnings.append('GSM_GATEWAY_SIP_USER and GSM_GATEWAY_SIP_PASSWORD are required for '
                                        'GSM_GATEWAY_MODE=register (the gateway authenticates to Asterisk)')
            elif len(self.gsm_gateway_sip_password) < 12 or self.gsm_gateway_sip_password.lower() in WEAK_SECRETS:
                gateway_warnings.append('GSM_GATEWAY_SIP_PASSWORD is weak (use 12+ random characters)')
        if not (1024 <= self.asterisk_rtp_start < self.asterisk_rtp_end <= 65535) or self.asterisk_rtp_start % 2:
            errors.append('ASTERISK_RTP_START must be even and lower than ASTERISK_RTP_END (1024-65535)')
        warnings.extend(f'gateway: {w}' for w in gateway_warnings)

        # Where Asterisk fetches dynamic Fish audio --------------------------------------------------------
        fastapi_base_url, base_error = parse_http_origin(self.fastapi_base_url, 'FASTAPI_BASE_URL')
        if base_error:
            errors.append(base_error)
        if self.asterisk_enabled and self.fish_speech_live_synthesis and not fastapi_base_url:
            errors.append('FISH_SPEECH_LIVE_SYNTHESIS=true with Asterisk requires FASTAPI_BASE_URL (Asterisk fetches '
                          'dynamic Fish audio from it)')

        # Speech recognition -------------------------------------------------------------------------------
        if self.stt_provider not in STT_PROVIDERS:
            errors.append(f'STT_PROVIDER must be one of {", ".join(STT_PROVIDERS)}')
        elif self.stt_provider == 'disabled' and (production_like or self.asterisk_enabled):
            (errors if production_like else warnings).append(
                'STT_PROVIDER=disabled: the receptionist cannot understand callers')
        if not 3 <= self.stt_record_max_seconds <= 60:
            errors.append('STT_RECORD_MAX_SECONDS must be between 3 and 60')
        if not 1 <= self.stt_record_max_silence_seconds <= 10:
            errors.append('STT_RECORD_MAX_SILENCE_SECONDS must be between 1 and 10')

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
            (errors if production_like or self.asterisk_enabled or twilio_required else warnings).append(
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
        if self.live_call_allowed and production_like and not allowed:
            warnings.append('LIVE_CALL_ALLOWED=true without OUTBOUND_ALLOWED_DESTINATIONS: consent, do-not-call and '
                            'opt-out rules are the only destination controls')

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
            asterisk_enabled=self.asterisk_enabled,
            live_call_allowed=self.live_call_allowed, ari_base_url=ari_base_url, ari_events_url=ari_events_url,
            sip_endpoint=self.asterisk_sip_endpoint, caller_id=caller_id, fastapi_base_url=fastapi_base_url,
            public_origin=public_origin, from_number=from_number, outbound_url=outbound_url, gather_url=gather_url,
            status_callback_url=status_callback_url, allowed_destinations=allowed, quota_seconds=quota,
            errors=errors, warnings=warnings,
            gateway_warnings=gateway_warnings,
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
