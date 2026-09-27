"""Live-call readiness report (``GET /api/twilio/preflight``).

Diagnostic: ``POST /api/calls/request`` does not consult it (Twilio's own answer decides), so it explains
what Twilio will accept or reject rather than blocking a request. It is never forced true.

``LIVE_CALL_ALLOWED`` is computed, never configured: it is true only when the real Twilio account
authenticates, the From number and the destination are authorised, the account may run the
application's own webhook (Full account), the public webhook is reachable and signature-protected,
Fish Speech and the verified voice pack are ready, packaged audio is publicly served, PostgreSQL is
ready, and the destination passes consent / do-not-call / opt-out, quota and capacity checks.
Remote checks (Twilio REST and the public origin) run only with ``verify_remote=1``.
"""
from __future__ import annotations

import asyncio
import time

from ..core.config import mask_phone, normalize_e164

_REMOTE_CACHE_SECONDS = 30.0
_remote_cache: dict[tuple, tuple[float, dict]] = {}


async def _cached(key: tuple, max_age: float, factory):
    now = time.monotonic()
    hit = _remote_cache.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    value = await factory()
    _remote_cache[key] = (time.monotonic(), value)
    return value


async def live_call_readiness(runtime, destination: str | None = None, *, verify_remote: bool = False,
                              max_age: float = _REMOTE_CACHE_SECONDS) -> dict:
    s = runtime.settings
    cfg = s.resolve()
    repo = runtime.repo
    pack = runtime.voice_pack()
    db_ok = await asyncio.to_thread(repo.healthy)
    migrations = await asyncio.to_thread(repo.migration_status) if db_ok else {}
    fish = await runtime.fish_health()
    usage = await asyncio.to_thread(repo.quota_usage, runtime.telephony.policy()) if db_ok else None
    raw_destination = destination or s.twilio_test_to or (cfg.allowed_destinations[0] if cfg.allowed_destinations else '')
    dest = normalize_e164(raw_destination, s.default_country_code) if raw_destination else None
    compliance = await asyncio.to_thread(repo.compliance_status, dest) if dest and db_ok else None
    configured = runtime.telephony.configured()

    account: dict = {'TWILIO_AUTHENTICATED': False, 'ACCOUNT_TYPE': 'unknown', 'ACCOUNT_STATUS': 'unknown',
                     'OWNED_TWILIO_NUMBER_COUNT': None, 'FROM_OWNED': False, 'FROM_VOICE_CAPABLE': False,
                     'FROM_AUTHORIZED': False, 'DESTINATION_AUTHORIZED': False, 'TRIAL_RESTRICTION': None,
                     'TRIAL_CALL_ALLOWED': False, 'CUSTOM_WEBHOOK_CALL_ALLOWED': False, 'ACCOUNT_BLOCKERS': []}
    public: dict = {'PUBLIC_HEALTH': False, 'PUBLIC_WEBHOOK_REACHABLE': False, 'PUBLIC_WEBHOOK_REJECTS_UNSIGNED': False,
                    'PUBLIC_AUDIO_READY': False}
    if verify_remote:
        account = await _cached(('account', dest), max_age, lambda: runtime.telephony.account_check(dest))
        public = await _cached(('public', cfg.public_origin), max_age, runtime.telephony.probe_public)
        public = dict(public)
        public['PUBLIC_HEALTH'] = bool(public.get('PUBLIC_HEALTH')
                                       and public.get('PUBLIC_INSTANCE_ID') == getattr(runtime, 'instance_id', None))

    checksum_errors = ('manifest_invalid', 'manifest_missing')
    checksum_valid = pack.manifest_present and not any(
        e in checksum_errors or e.startswith(('hash:', 'missing:', 'manifest_entry_missing:')) for e in pack.errors)
    database_ready = bool(db_ok and not migrations.get('pending') and not migrations.get('modified'))
    postgres_ready = repo.backend == 'postgresql' and database_ready
    signature_ready = bool(s.twilio_validate_signature and s.twilio_auth_token and cfg.public_origin
                           and (public.get('PUBLIC_WEBHOOK_REJECTS_UNSIGNED') if verify_remote else True))
    packaged_audio_ready = bool(pack.valid and runtime.public_asset_url('outbound_greeting')
                                and (public.get('PUBLIC_AUDIO_READY') if verify_remote else True))
    checks: dict = {
        'APPLICATION_MODE': cfg.app_mode,
        'CALL_PROVIDER': cfg.call_provider,
        'TWILIO_ENABLED': s.twilio_enabled,
        'TWILIO_CONFIGURED': configured['TWILIO_CONFIGURED'],
        'TWILIO_AUTHENTICATED': account['TWILIO_AUTHENTICATED'],
        'ACCOUNT_TYPE': account['ACCOUNT_TYPE'],
        'ACCOUNT_STATUS': account['ACCOUNT_STATUS'],
        'OWNED_TWILIO_NUMBER_COUNT': account['OWNED_TWILIO_NUMBER_COUNT'],
        'FROM_CONFIGURED': configured['FROM_CONFIGURED'],
        'FROM_OWNED': account['FROM_OWNED'],
        'FROM_VOICE_CAPABLE': account['FROM_VOICE_CAPABLE'],
        'FROM_AUTHORIZED': account['FROM_AUTHORIZED'],
        'DESTINATION': mask_phone(dest) if dest else None,
        'DESTINATION_VALID': bool(dest),
        'DESTINATION_COUNTRY': account.get('DESTINATION_COUNTRY'),
        'DESTINATION_AUTHORIZED': account['DESTINATION_AUTHORIZED'],
        'TRIAL_RESTRICTION': account['TRIAL_RESTRICTION'],
        'TRIAL_CALL_ALLOWED': account['TRIAL_CALL_ALLOWED'],
        'CUSTOM_WEBHOOK_CALL_ALLOWED': account['CUSTOM_WEBHOOK_CALL_ALLOWED'],
        'PUBLIC_BASE_URL_CONFIGURED': configured['PUBLIC_BASE_URL_CONFIGURED'],
        'PUBLIC_HEALTH': public['PUBLIC_HEALTH'],
        'PUBLIC_WEBHOOK_REACHABLE': public['PUBLIC_WEBHOOK_REACHABLE'],
        'TWILIO_SIGNATURE_VALIDATION_READY': signature_ready,
        'FISH_SPEECH_READY': fish.ready,
        'VOICE_PACK_COMPLETE': pack.complete,
        'VOICE_PACK_VALID': pack.valid,
        'VOICE_PACK_CHECKSUM_VALID': checksum_valid,
        'VOICE_PACK_ASSET_COUNT': len(pack.assets),
        'PACKAGED_AUDIO_RUNTIME_READY': packaged_audio_ready,
        'POSTGRES_READY': postgres_ready,
        'DATABASE_READY': database_ready,   # call records usable on the configured backend (any)
        'DATABASE_BACKEND': repo.backend,
        'QUOTA_REMAINING_SECONDS': usage.remaining_seconds if usage else None,
        'ACTIVE_CALLS': usage.active_calls if usage else None,
        'CONSENT_RECORDED': compliance['consent'] if compliance else None,
        'DO_NOT_CALL': compliance['do_not_call'] if compliance else None,
        'OPTED_OUT': compliance['opted_out'] if compliance else None,
        'REMOTE_CHECKS_RUN': verify_remote,
    }

    software: list[str] = [f'config: {error}' for error in cfg.errors]
    if not s.twilio_enabled:
        software.append('TWILIO_DISABLED: TWILIO_ENABLED=false')
    if not configured['TWILIO_CONFIGURED']:
        software.append('TWILIO_CREDENTIALS_NOT_CONFIGURED: TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN are empty in .env')
    if not configured['PUBLIC_BASE_URL_CONFIGURED']:
        software.append('PUBLIC_BASE_URL_NOT_CONFIGURED')
    software.extend(runtime.voice_blockers())
    if not postgres_ready:
        software.append('POSTGRES_NOT_READY' + ('' if db_ok else ': database unreachable'))
    if not fish.ready:
        software.append(f'FISH_SPEECH_NOT_READY: {fish.error or "not ready"}')
    if not checksum_valid:
        software.append('VOICE_PACK_CHECKSUM_INVALID')
    if not signature_ready:
        software.append('TWILIO_SIGNATURE_VALIDATION_NOT_READY')
    if not packaged_audio_ready:
        software.append('PACKAGED_AUDIO_NOT_PUBLICLY_SERVED')
    if usage is not None:
        if usage.remaining_seconds < s.twilio_max_call_seconds:
            software.append('QUOTA_EXHAUSTED')
        if usage.active_calls >= s.max_concurrent_app_calls:
            software.append('CONCURRENCY_LIMIT_REACHED')
    if verify_remote:
        if not public['PUBLIC_HEALTH']:
            software.append('PUBLIC_HEALTH_FAILED: PUBLIC_BASE_URL does not reach this FastAPI process')
        if not public['PUBLIC_WEBHOOK_REACHABLE']:
            software.append('PUBLIC_WEBHOOK_UNREACHABLE')
    else:
        software.append('REMOTE_CHECKS_NOT_RUN: pass verify_remote=1')

    account_blockers = list(account.get('ACCOUNT_BLOCKERS') or [])
    destination_blockers: list[str] = []
    if raw_destination and not dest:
        destination_blockers.append('DESTINATION_INVALID')
    elif not dest:
        destination_blockers.append('NO_DESTINATION: pass ?destination= or set TWILIO_TEST_TO')
    else:
        if cfg.allowed_destinations and dest not in cfg.allowed_destinations:
            destination_blockers.append('DESTINATION_NOT_ALLOWLISTED')
        if compliance:
            if not compliance['consent']:
                destination_blockers.append('CONSENT_NOT_RECORDED')
            if compliance['do_not_call']:
                destination_blockers.append('DESTINATION_ON_DO_NOT_CALL_LIST')
            if compliance['opted_out']:
                destination_blockers.append('DESTINATION_OPTED_OUT')

    gate = all(checks[key] is True for key in (
        'TWILIO_AUTHENTICATED', 'FROM_AUTHORIZED', 'DESTINATION_AUTHORIZED', 'CUSTOM_WEBHOOK_CALL_ALLOWED',
        'PUBLIC_WEBHOOK_REACHABLE', 'TWILIO_SIGNATURE_VALIDATION_READY', 'FISH_SPEECH_READY', 'VOICE_PACK_COMPLETE',
        'VOICE_PACK_VALID', 'PACKAGED_AUDIO_RUNTIME_READY', 'POSTGRES_READY'))
    checks['CONFIG_WARNINGS'] = cfg.warnings
    checks['SOFTWARE_BLOCKERS'] = software
    checks['ACCOUNT_BLOCKERS'] = account_blockers
    checks['DESTINATION_BLOCKERS'] = destination_blockers
    checks['BLOCKERS'] = software + account_blockers + destination_blockers
    checks['SOFTWARE_READY_FOR_LIVE_CALL'] = not software
    checks['LIVE_CALL_ALLOWED'] = bool(verify_remote and gate and not checks['BLOCKERS'])
    checks['CHECKED_AT'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    checks['DETAILS'] = {'voice_pack': pack.summary(), 'fish_speech': fish.as_dict(), 'twilio': runtime.telephony.health(),
                         'public': public, 'quota': usage.as_dict() if usage else None, 'migrations': migrations}
    return checks
