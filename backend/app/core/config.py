"""Application settings with one authoritative, explainable effective value per key.

Precedence is the standard pydantic-settings order: process environment, then the
project-root .env file, then the defaults below. ``Settings.resolve()`` turns the raw
values into the effective runtime configuration and reports contradictions as
errors instead of silently rewriting them.

Telephony runs on open-source Asterisk (AMI for call control, FastAGI for the
conversation). Asterisk itself runs under WSL on this Windows host, so paths that
Asterisk opens are expressed as WSL paths.
"""
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / '.env'
APPROVED_KNOWLEDGE_PATH = PROJECT_ROOT / 'knowledge' / 'clinic' / 'approved.json'
VOICE_PROMPTS_PATH = PROJECT_ROOT / 'knowledge' / 'clinic' / 'voice_prompts.json'
APPROVED_FACTS = json.loads(APPROVED_KNOWLEDGE_PATH.read_text(encoding='utf-8'))['facts']
APPROVED_CLINIC_PHONE = APPROVED_FACTS['phone']
E164 = re.compile(r'^\+[1-9]\d{7,14}$')
TELEPHONY_INTERFACES = ('sip', 'gsm')
DEFAULT_CHANNEL_TEMPLATES = {'sip': 'PJSIP/{e164}@pstn-trunk', 'gsm': 'Dongle/dongle0/{e164}'}
_CHANNEL_TEMPLATE = re.compile(r'^[A-Za-z0-9_]+/[^\s]*\{(?:e164|digits)\}[^\s]*$')


def _should_ignore_env_file() -> bool:
    return 'PYTEST_CURRENT_TEST' in os.environ or any('pytest' in arg.lower() for arg in sys.argv)


def _digits(value: str) -> str:
    return ''.join(ch for ch in value or '' if ch.isdigit())


def mask_phone(value: str | None) -> str:
    value = (value or '').strip()
    if len(value) <= 6:
        return '***' if value else ''
    return value[:3] + '*' * (len(value) - 7) + value[-4:]


def to_wsl_path(path: Path | str) -> str:
    """Windows path as seen from WSL (E:\\a\\b -> /mnt/e/a/b)."""
    p = PureWindowsPath(str(path))
    if not p.drive or not p.drive.endswith(':'):
        return str(path).replace('\\', '/')
    return '/mnt/' + p.drive[0].lower() + '/' + '/'.join(p.parts[1:])


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
    try:
        address = ipaddress.ip_address(host)
        private = address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified
    except ValueError:
        private = host in {'localhost'} or host.endswith(('.local', '.localhost', '.internal'))
    if not host or private:
        return '', f'{name} must be a public hostname (got {host or "none"})'
    if parsed.path not in {'', '/'} or parsed.params or parsed.query or parsed.fragment:
        return '', (f'{name} must be the public origin only; remove the path {parsed.path!r}. '
                    'UI routes such as /demo are separate from API routes.')
    netloc = host if port in (None, 443) else f'{host}:{port}'
    return f'https://{netloc}', None


@dataclass
class ResolvedConfig:
    app_mode: str
    mock_mode: bool
    call_provider: str
    telephony_interface: str
    channel_template: str
    caller_id: str
    public_origin: str
    allowed_destinations: list[str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return not self.mock_mode

    @property
    def asterisk_enabled(self) -> bool:
        return self.live and self.call_provider == 'asterisk'


class Settings(BaseSettings):
    app_name: str = 'Mouth Care Solutions AI Receptionist'
    app_mode: str = 'demo'
    mock_mode: bool = True
    clinic_email: str = APPROVED_FACTS['email']
    clinic_phone: str = APPROVED_CLINIC_PHONE
    admin_demo_token: str = 'demo-admin-token'
    admin_initial_email: str = ''
    admin_initial_password: str = ''
    jwt_secret: str = 'change-me-in-live-mode'
    pilot_minutes: int = 3000
    database_url: str = ''
    call_state_db_path: str = 'data/call_state.sqlite3'
    llm_provider: str = 'openai-compatible'
    llm_api_key: str = ''
    openai_api_key: str = ''
    openai_api_base: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4o-mini'
    frontend_api_base_url: str = 'http://localhost:8000'
    vite_api_base_url: str = ''
    notification_provider: str = 'mock'
    public_base_url: str = ''

    # Telephony: open-source Asterisk ------------------------------------------------
    call_provider: str = 'mock'
    asterisk_wsl_distro: str = 'Ubuntu'
    asterisk_ami_host: str = '127.0.0.1'
    asterisk_ami_port: int = 5038
    asterisk_ami_username: str = 'mouthcare'
    asterisk_ami_secret: str = ''
    asterisk_context: str = 'mouthcare-receptionist'
    asterisk_agi_bind: str = '127.0.0.1'   # where this app's FastAGI server listens
    asterisk_agi_host: str = '127.0.0.1'   # address Asterisk dials to reach it (WSL mirrored networking)
    asterisk_agi_port: int = 4573
    asterisk_voice_dir: str = ''          # Asterisk-side path of the verified voice pack (default: WSL view of the project pack)
    asterisk_recording_dir: str = '/var/spool/asterisk/mouthcare-recordings'
    local_recording_dir: str = ''         # Windows view of ASTERISK_RECORDING_DIR (default: \\wsl.localhost\<distro>\...)
    telephony_interface: str = ''         # 'sip' (PJSIP trunk) or 'gsm' (chan_dongle modem); empty = none configured
    outbound_channel_template: str = ''   # e.g. PJSIP/{e164}@pstn-trunk
    outbound_caller_id: str = ''
    sip_trunk_endpoint: str = 'pstn-trunk'
    sip_trunk_host: str = ''
    sip_trunk_username: str = ''
    sip_trunk_password: str = ''
    sip_trunk_requires_registration: bool = True
    gsm_device: str = 'dongle0'
    outbound_allowed_destinations: str = ''
    outbound_rate_limit_seconds: int = 300
    outbound_ring_timeout_seconds: int = 45
    max_call_turns: int = 24
    asr_model: str = 'base.en'
    asr_record_timeout_seconds: int = 10
    asr_silence_seconds: int = 2

    # Fish Speech reference voice ------------------------------------------------------
    fish_speech_enabled: bool = True
    fish_speech_base_url: str = 'http://127.0.0.1:8080'
    fish_speech_model: str = 'fish-speech-1.5'
    fish_speech_reference_audio: str = 'fish-references/mouth-care-receptionist-reference-20260919.wav'
    fish_speech_reference_text: str = 'Tomorrow is holiday because of Sunday. The Sunday is because of today is Saturday. Today is Saturday is because of yesterday is Friday. Friday is because of Thursday. But I know you are not willing to listen, but you have to listen.'
    fish_speech_reference_id: str = ''
    fish_speech_output_format: str = 'wav'
    fish_speech_sample_rate: int = 8000
    fish_speech_channels: int = 1
    fish_speech_timeout_seconds: float = 900.0
    fish_speech_max_new_tokens: int = 1024
    fish_speech_audio_ttl_seconds: int = 300
    fish_speech_voice_pack_dir: str = 'artifacts/voice-pack'
    model_config = SettingsConfigDict(
        env_file=None if _should_ignore_env_file() else ENV_FILE,
        env_file_encoding='utf-8',
        env_ignore_empty=True,
        extra='ignore',
    )

    def project_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def asterisk_voice_path(self) -> str:
        return self.asterisk_voice_dir.rstrip('/') or to_wsl_path(self.project_path(self.fish_speech_voice_pack_dir))

    def local_recording_path(self) -> Path:
        if self.local_recording_dir:
            return Path(self.local_recording_dir)
        return Path('\\\\wsl.localhost\\' + self.asterisk_wsl_distro + self.asterisk_recording_dir.replace('/', '\\'))

    def resolve(self) -> ResolvedConfig:
        errors: list[str] = []
        warnings: list[str] = []
        mode = (self.app_mode or '').strip().lower()
        live = not self.mock_mode
        if mode == 'live' and self.mock_mode:
            errors.append('APP_MODE=live conflicts with MOCK_MODE=true')
        if live and mode != 'live':
            errors.append(f'MOCK_MODE=false requires APP_MODE=live (got APP_MODE={mode or "unset"})')

        provider = (self.call_provider or '').strip().lower()
        if self.mock_mode:
            if provider not in {'', 'mock'}:
                warnings.append(f'CALL_PROVIDER={provider} is inactive because MOCK_MODE=true')
            provider = 'mock'
        elif provider != 'asterisk':
            errors.append(f'live mode requires CALL_PROVIDER=asterisk (got {provider or "unset"})')

        interface = (self.telephony_interface or '').strip().lower()
        if interface and interface not in TELEPHONY_INTERFACES:
            errors.append(f'TELEPHONY_INTERFACE must be one of {", ".join(TELEPHONY_INTERFACES)} (got {interface})')
            interface = ''
        template = (self.outbound_channel_template or '').strip() or DEFAULT_CHANNEL_TEMPLATES.get(interface, '')
        if template and not _CHANNEL_TEMPLATE.match(template):
            errors.append('OUTBOUND_CHANNEL_TEMPLATE must look like Tech/...{e164}... or Tech/...{digits}...')
        caller_id = (self.outbound_caller_id or '').strip()
        if caller_id and not E164.match(caller_id):
            errors.append('OUTBOUND_CALLER_ID must be an E.164 number')
        if interface == 'sip' and not (self.sip_trunk_host and self.sip_trunk_username and self.sip_trunk_password):
            errors.append('TELEPHONY_INTERFACE=sip requires SIP_TRUNK_HOST, SIP_TRUNK_USERNAME and SIP_TRUNK_PASSWORD')

        public_origin, public_error = parse_public_origin(self.public_base_url, 'PUBLIC_BASE_URL')
        if public_error:
            errors.append(public_error)

        allowed = []
        for item in (d.strip() for d in self.outbound_allowed_destinations.split(',')):
            if not item:
                continue
            if E164.match(item):
                allowed.append(item)
            else:
                errors.append('OUTBOUND_ALLOWED_DESTINATIONS entries must be E.164 numbers')

        if live and not self.fish_speech_enabled:
            errors.append('live mode requires FISH_SPEECH_ENABLED=true (packaged Fish reference voice)')
        if live and provider == 'asterisk' and not self.asterisk_ami_secret:
            errors.append('ASTERISK_AMI_SECRET is required in live mode (run scripts/setup_asterisk.ps1)')
        if _digits(self.clinic_phone) != _digits(APPROVED_CLINIC_PHONE):
            warnings.append('CLINIC_PHONE differs from approved clinic knowledge and is ignored')
        if live and (self.jwt_secret in {'change-me-in-live-mode', 'replace-in-live-mode'} or len(self.jwt_secret) < 32):
            warnings.append('JWT_SECRET is weak or a published placeholder; admin API stays disabled')
        if live and not (self.admin_initial_email and self.admin_initial_password):
            warnings.append('ADMIN_INITIAL_EMAIL/ADMIN_INITIAL_PASSWORD unset; admin API is disabled')
        if live:
            warnings.append('live appointment scheduling is disabled: no production booking repository is connected')
        return ResolvedConfig(
            app_mode=mode, mock_mode=self.mock_mode, call_provider=provider, telephony_interface=interface,
            channel_template=template, caller_id=caller_id, public_origin=public_origin,
            allowed_destinations=allowed, errors=errors, warnings=warnings,
        )

    def validate_live(self) -> list[str]:
        """Blocking configuration errors for the live voice-agent path (empty in demo mode)."""
        return [] if self.mock_mode else self.resolve().errors

    def value_sources(self) -> dict[str, str]:
        """Where each effective value came from: process env, the .env file, or the default."""
        file_keys = set()
        env_file = self.model_config.get('env_file')
        if env_file and Path(env_file).is_file():
            for line in Path(env_file).read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    if value.strip():
                        file_keys.add(key.strip().upper())
        return {
            name: 'process_env' if os.environ.get(name.upper(), '').strip() else '.env' if name.upper() in file_keys else 'default'
            for name in type(self).model_fields
        }

    def env_file_diagnostics(self) -> dict:
        """Duplicate, blank and unknown keys in the .env file (names only, never values)."""
        env_file = self.model_config.get('env_file')
        result = {'duplicates': [], 'blank': [], 'unknown': []}
        if not env_file or not Path(env_file).is_file():
            return result
        seen = set()
        known = {name.upper() for name in type(self).model_fields}
        for line in Path(env_file).read_text(encoding='utf-8').splitlines():
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
