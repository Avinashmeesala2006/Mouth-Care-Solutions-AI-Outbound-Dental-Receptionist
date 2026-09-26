from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
import base64, hashlib, hmac, logging, re, secrets, sys, threading, time
from fastapi.responses import HTMLResponse, JSONResponse, Response
import httpx
from .schemas.api import *
from .services.repository import Repo
from .services.engine import Agent, CLINIC, SERVICES
from .services.call_store import CallStore
from .services.voice_pack import load_prompts, load_voice_pack
from .core.config import settings, mask_phone, E164, VOICE_PROMPTS_PATH
from .telephony.agi import AGIServer, ReceptionistAGI
from .telephony.asr import Transcriber
from .telephony.asterisk import AMIEventListener, AsteriskTelephonyProvider
from .telephony.base import TelephonyError
from .auth import hash_password, verify_password
from .demo_page import DEMO_HTML

logger = logging.getLogger(__name__)
_app_logger = logging.getLogger('backend.app')
if not _app_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    _app_logger.addHandler(_handler)
    _app_logger.setLevel(logging.INFO)

INSTANCE_ID = secrets.token_hex(8)
repo=Repo(); agent=Agent(repo)
_store_path = settings.call_state_db_path if settings.call_state_db_path == ':memory:' else str(settings.project_path(settings.call_state_db_path))
store = CallStore(_store_path)
_call_lock = threading.Lock()
_ASSET_ID = re.compile(r'^[a-z][a-z_]{1,63}$')


# Voice pack ----------------------------------------------------------------------
_voice_lock = threading.Lock()
_voice_cache: dict = {'signature': None, 'status': None}


def _voice_files_signature() -> tuple:
    pack_dir = settings.project_path(settings.fish_speech_voice_pack_dir)
    reference = settings.project_path(settings.fish_speech_reference_audio)
    paths = [VOICE_PROMPTS_PATH, reference, *(sorted(pack_dir.iterdir()) if pack_dir.is_dir() else [])]
    items = []
    for path in paths:
        try:
            stat = path.stat()
            items.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            items.append((str(path), None, None))
    return tuple(items)


def voice_pack():
    """Current validated voice pack; reloaded when any pack, prompt or reference file changes."""
    signature = _voice_files_signature()
    with _voice_lock:
        if _voice_cache['signature'] != signature:
            status = load_voice_pack(
                settings.project_path(settings.fish_speech_voice_pack_dir),
                load_prompts(VOICE_PROMPTS_PATH),
                settings.project_path(settings.fish_speech_reference_audio),
            )
            _voice_cache.update(signature=signature, status=status)
            logger.info('voice_pack_loaded valid=%s verified=%d/%d errors=%s', status.valid, len(status.assets), len(status.required), status.errors[:8])
        return _voice_cache['status']


voice_pack()


# Telephony (open-source Asterisk) ------------------------------------------------------
provider = AsteriskTelephonyProvider(settings, store)
transcriber = Transcriber(settings.asr_model)
agi_server = AGIServer(ReceptionistAGI(settings, store, lambda: voice_pack(), transcriber),
                       settings.asterisk_agi_bind, settings.asterisk_agi_port)
event_listener = AMIEventListener(provider)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.resolve().asterisk_enabled:
        transcriber.load_in_background()
        agi_server.start()
        event_listener.start()
        logger.info('telephony_started agi=%s:%s status=%s', agi_server.host, agi_server.port, agi_server.status)
    yield
    event_listener.stop()
    agi_server.stop()


app=FastAPI(title='Mouth Care Solutions AI Receptionist', version='3.0.0', lifespan=lifespan)


# Helpers --------------------------------------------------------------------------
def _fish_ready() -> bool:
    if not (settings.fish_speech_enabled and settings.fish_speech_base_url):
        return False
    try:
        return httpx.get(f"{settings.fish_speech_base_url.rstrip('/')}/v1/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def _normalize_e164(value: str) -> str | None:
    raw = (value or '').strip()
    if any(ch.isdigit() and not ch.isascii() for ch in raw):
        return None
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if raw.startswith('+'):
        candidate = '+' + digits
    elif len(digits) == 10:
        candidate = '+91' + digits
    elif len(digits) == 11 and digits.startswith('0'):
        candidate = '+91' + digits[1:]
    elif len(digits) == 12 and digits.startswith('91'):
        candidate = '+' + digits
    else:
        return None
    return candidate if E164.match(candidate) else None


# Admin auth -----------------------------------------------------------------------
_demo_admin_hash = hash_password('demo-password')
_ADMIN_TOKEN_TTL = 12 * 3600
_WEAK_JWT_SECRETS = {'', 'change-me-in-live-mode', 'replace-in-live-mode', 'changeme', 'secret'}


def _sign(payload: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _session_token(role='admin'):
    payload = f'{role}|{int(time.time()) + _ADMIN_TOKEN_TTL}|{secrets.token_hex(8)}'
    return 'mcs.' + base64.urlsafe_b64encode(payload.encode()).decode() + '.' + _sign(payload)


def _admin_status() -> tuple[bool, str]:
    """Live admin requires explicit credentials and a strong signing secret; otherwise it stays off."""
    if settings.mock_mode:
        return True, 'demo'
    if not (settings.admin_initial_email and settings.admin_initial_password):
        return False, 'ADMIN_INITIAL_EMAIL/ADMIN_INITIAL_PASSWORD are not set'
    if settings.jwt_secret in _WEAK_JWT_SECRETS or len(settings.jwt_secret) < 32:
        return False, 'JWT_SECRET is weak (use at least 32 random characters)'
    if len(settings.admin_initial_password) < 12:
        return False, 'ADMIN_INITIAL_PASSWORD is shorter than 12 characters'
    return True, 'configured'


def _admin_allowed(authorization):
    if settings.mock_mode and authorization == 'Bearer demo-admin-token': return True
    if not _admin_status()[0]: return False
    if not authorization or not authorization.startswith('Bearer mcs.'): return False
    try:
        _, encoded, sig = authorization.split('.', 2)
        payload = base64.urlsafe_b64decode(encoded).decode()
        role, expires, _nonce = payload.split('|', 2)
        return hmac.compare_digest(sig, _sign(payload)) and role == 'admin' and int(expires) > time.time()
    except (ValueError, UnicodeDecodeError):
        return False


def _require_admin(authorization):
    if not _admin_allowed(authorization):
        raise HTTPException(403, 'admin authorization required')


def err(e): raise HTTPException(400,str(e))


# Public pages and health ----------------------------------------------------------
@app.get('/', response_class=HTMLResponse)
def root(): return HTMLResponse('<meta http-equiv="refresh" content="0; url=/demo"><a href="/demo">Open Mouth Care Solutions receptionist demo</a>')
@app.get('/favicon.ico')
def favicon(): return HTMLResponse('', status_code=204)
@app.get('/demo',response_class=HTMLResponse)
def demo(): return HTMLResponse(DEMO_HTML)


@app.get('/health')
def health(): return {'status':'ok','mode':'demo' if settings.mock_mode else 'live','instance_id':INSTANCE_ID}


@app.get('/ready')
def ready():
    cfg = settings.resolve()
    pack = voice_pack()
    store_ok = store.healthy()
    fish_ready = _fish_ready()
    live_ready = not cfg.errors and pack.valid and store_ok and fish_ready
    ready_now = live_ready if cfg.live else store_ok
    body = {
        'status': 'ready' if ready_now else 'not_ready',
        'scope': 'application (live-call readiness: GET /api/telephony/preflight)',
        'mode': 'live' if cfg.live else 'demo',
        'config_errors': cfg.errors,
        'config_warnings': cfg.warnings,
        'call_state': {'backend': 'sqlite', 'path': store.path, 'healthy': store_ok},
        'booking_repository': 'in-memory-demo' if settings.mock_mode else 'unavailable-live',
        'booking_repository_ready': settings.mock_mode,
        'providers': {'voice': 'fish-speech-voice-pack', 'notifications': settings.notification_provider, 'telephony': cfg.call_provider},
        'telephony': {'agi_server': agi_server.status, 'asr': transcriber.status, 'ami_events': event_listener.connected},
        'fish_speech': {'configured': bool(settings.fish_speech_enabled and settings.fish_speech_base_url), 'reachable': fish_ready, 'required_during_calls': False},
        'voice_pack': pack.summary(),
    }
    return JSONResponse(body, status_code=200 if ready_now else 503)


def _packaged_audio(asset_id: str, include_body: bool) -> Response:
    pack = voice_pack()
    asset = pack.assets.get(asset_id) if _ASSET_ID.match(asset_id) else None
    if not asset:
        raise HTTPException(404, 'audio_not_found')
    return Response(content=asset.content if include_body else b'', media_type='audio/wav',
                    headers={'Cache-Control': 'no-store', 'X-Voice-Asset-SHA256': asset.sha256, 'Content-Length': str(len(asset.content))})


@app.get('/api/telephony/audio/{asset_id}')
def packaged_audio(asset_id: str):
    """Verified Fish voice assets (read-only), for listening checks and the web client."""
    return _packaged_audio(asset_id, include_body=True)


@app.head('/api/telephony/audio/{asset_id}', include_in_schema=False)
def packaged_audio_head(asset_id: str):
    return _packaged_audio(asset_id, include_body=False)


# Web receptionist and demo booking ------------------------------------------------
def _agent_response(message: str, session_id: str) -> dict:
    result = agent.respond(message, session_id)
    if not settings.mock_mode and result.get('intent') == 'appointment':
        return {
            'text': 'I can record an appointment request for the clinic team to confirm, but live appointment availability is not connected.',
            'intent': 'appointment_request',
            'session_id': session_id,
        }
    return result


def _require_booking_repository() -> None:
    if not settings.mock_mode:
        raise HTTPException(503, detail={
            'error': 'live_booking_repository_unavailable',
            'message': 'Live appointment availability is not connected.',
        })


@app.post('/api/agent/session')
def session(r:AgentRequest): return _agent_response(r.message,r.session_id or 'demo-session')
@app.post('/api/knowledge/search')
def search(r:KnowledgeRequest): return _agent_response(r.query,'knowledge-search')
@app.get('/api/slots')
def slots():
    _require_booking_repository()
    return {'slots':[repo.slot_view(s) for s in repo.available()]}
@app.post('/api/booking/hold')
def hold(r:HoldRequest):
 _require_booking_repository()
 try:return repo.hold(r.slot_id,r.session_id,r.expiry_seconds)
 except ValueError as e: err(e)
@app.post('/api/booking/confirm')
def confirm(r:ConfirmRequest):
 _require_booking_repository()
 try:return repo.confirm(r.hold_token,r.patient_details,r.consent,r.idempotency_key)
 except ValueError as e: err(e)
@app.post('/api/booking/reschedule')
def reschedule(r:RescheduleRequest):
 _require_booking_repository()
 try:return repo.reschedule(r.booking_reference,r.new_slot_id,r.verification)
 except ValueError as e: err(e)
@app.post('/api/booking/cancel')
def cancel(r:CancelRequest):
 _require_booking_repository()
 try:return repo.cancel(r.booking_reference,r.reason,r.verification)
 except ValueError as e: err(e)
@app.post('/api/callback')
def callback(r:CallbackRequest):
    item = store.add_callback(source='web', contact=r.patient_contact, topic=r.topic, preferred_window=r.preferred_window)
    repo.log('callback_created', {'id': item['id']})
    return {'id': item['id'], 'patient_contact': r.patient_contact, 'topic': r.topic, 'preferred_window': r.preferred_window, 'status': item['status']}


# Live-call preflight -----------------------------------------------------------------
def _telephony_preflight(destination: str | None = None, refresh: bool = False) -> dict:
    cfg = settings.resolve()
    dest = destination or (cfg.allowed_destinations[0] if cfg.allowed_destinations else '')
    pack = voice_pack()
    store_ok = store.healthy()
    fish_ready = _fish_ready()
    try:
        telephony = provider.preflight(dest, refresh=refresh)
    except Exception as exc:  # an inspection crash is a blocker, never a pass
        logger.error('telephony_preflight_failed exception_type=%s', type(exc).__name__)
        telephony = {'SOFTWARE_BLOCKERS': [f'TELEPHONY_PREFLIGHT_FAILED: {type(exc).__name__}'], 'INTERFACE_BLOCKERS': [], 'DETAILS': {}}
    checksum_errors = ('manifest_invalid', 'manifest_missing')
    checksum_prefixes = ('hash:', 'missing:', 'manifest_entry_missing:')
    checksum_valid = pack.manifest_present and not any(e in checksum_errors or e.startswith(checksum_prefixes) for e in pack.errors)
    destination_valid = bool(dest) and bool(E164.match(dest))
    checks = {
        'APPLICATION_MODE': cfg.app_mode,
        'MOCK_MODE': cfg.mock_mode,
        'CALL_PROVIDER': cfg.call_provider,
        **{k: v for k, v in telephony.items() if k not in {'SOFTWARE_BLOCKERS', 'INTERFACE_BLOCKERS', 'DETAILS'}},
        'AGI_SERVER_READY': agi_server.listening,
        'ASR_READY': transcriber.ready,
        'ASR_STATUS': transcriber.status,
        'AMI_EVENTS_CONNECTED': event_listener.connected,
        'DESTINATION': mask_phone(dest),
        'DESTINATION_VALID': destination_valid,
        'DESTINATION_ALLOWLISTED': destination_valid and dest in cfg.allowed_destinations,
        'FISH_SPEECH_READY': fish_ready,
        'VOICE_PACK_COMPLETE': pack.complete,
        'VOICE_PACK_VALID': pack.valid,
        'VOICE_PACK_CHECKSUM_VALID': checksum_valid,
        'PACKAGED_AUDIO_RUNTIME_READY': bool(pack.valid and telephony.get('ASTERISK_AUDIO_FORMAT_READY') and agi_server.listening),
        'CALL_STATE_STORE_READY': store_ok,
        'BOOKING_REPOSITORY_READY': settings.mock_mode,
    }
    software = []
    if cfg.mock_mode:
        software.append('MOCK_MODE=true: demo mode never places real calls')
    software.extend(f'config: {error}' for error in cfg.errors)
    software.extend(telephony.get('SOFTWARE_BLOCKERS', []))
    if not agi_server.listening:
        software.append(f'AGI_SERVER_NOT_LISTENING: {agi_server.error or agi_server.status}')
    if not transcriber.ready:
        software.append(f'ASR_NOT_READY: {transcriber.error or transcriber.status}')
    if not fish_ready:
        software.append('FISH_SPEECH_NOT_REACHABLE')
    if not pack.valid:
        software.append('VOICE_PACK_INVALID: ' + ', '.join(pack.errors[:6]))
    if not store_ok:
        software.append('CALL_STATE_STORE_NOT_WRITABLE')
    if not dest:
        software.append('NO_DESTINATION: set OUTBOUND_ALLOWED_DESTINATIONS')
    elif not destination_valid:
        software.append('DESTINATION_NOT_E164')
    elif not checks['DESTINATION_ALLOWLISTED']:
        software.append('DESTINATION_NOT_ALLOWLISTED')
    interface = list(telephony.get('INTERFACE_BLOCKERS', []))
    admin_enabled, admin_reason = _admin_status()
    checks['ADMIN_STATUS'] = 'enabled' if admin_enabled else f'disabled: {admin_reason}'
    checks['CONFIG_WARNINGS'] = cfg.warnings
    checks['SOFTWARE_BLOCKERS'] = software
    checks['INTERFACE_BLOCKERS'] = interface
    checks['BLOCKERS'] = software + interface
    checks['SOFTWARE_READY_FOR_LIVE_CALL'] = not software
    checks['LIVE_CALL_ALLOWED'] = not software and not interface
    checks['CHECKED_AT'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    checks['DETAILS'] = {'telephony': telephony.get('DETAILS', {}), 'voice_pack': pack.summary(),
                         'fish_speech_required_during_calls': False}
    return checks


_PREFLIGHT_MAX_AGE = 120
_PREFLIGHT_REFRESH_AFTER = 60
_preflight_cache: dict = {'started': 0.0, 'at': 0.0, 'result': None, 'running': False}
_preflight_cache_lock = threading.Lock()


def _run_preflight(destination: str | None = None, refresh: bool = False) -> dict:
    started = time.monotonic()
    try:
        result = _telephony_preflight(destination, refresh=refresh)
    except Exception:
        if destination is None:
            with _preflight_cache_lock:
                _preflight_cache.update(result=None, at=0.0)  # unknown, never a stale green
        raise
    if destination is None:
        with _preflight_cache_lock:
            if started >= _preflight_cache['started']:
                _preflight_cache.update(started=started, at=time.monotonic(), result=result)
    return result


def _refresh_preflight_in_background() -> None:
    with _preflight_cache_lock:
        if _preflight_cache['running']:
            return
        _preflight_cache['running'] = True

    def worker():
        try:
            _run_preflight()
        except Exception as exc:
            logger.error('background_preflight_failed exception_type=%s', type(exc).__name__)
        finally:
            with _preflight_cache_lock:
                _preflight_cache['running'] = False
    threading.Thread(target=worker, name='preflight-refresh', daemon=True).start()


def _cached_readiness(live_checks: dict) -> dict | None:
    """Last full preflight, ANDed with cheap live checks; None when unknown or stale."""
    with _preflight_cache_lock:
        result, at = _preflight_cache['result'], _preflight_cache['at']
    if not result or time.monotonic() - at > _PREFLIGHT_MAX_AGE:
        return None
    keys = ('SOFTWARE_READY_FOR_LIVE_CALL', 'LIVE_CALL_ALLOWED', 'SOFTWARE_BLOCKERS', 'INTERFACE_BLOCKERS', 'BLOCKERS',
            'ASTERISK_INSTALLED', 'ASTERISK_RUNNING', 'ASTERISK_VERSION', 'TELEPHONY_INTERFACE', 'TELEPHONY_INTERFACE_READY',
            'CHECKED_AT')
    summary = {key: result.get(key) for key in keys}
    still_ok = all(live_checks.values())
    summary['SOFTWARE_READY_FOR_LIVE_CALL'] = bool(summary['SOFTWARE_READY_FOR_LIVE_CALL'] and still_ok)
    summary['LIVE_CALL_ALLOWED'] = bool(summary['LIVE_CALL_ALLOWED'] and still_ok)
    if not still_ok:
        failing = [name for name, ok in live_checks.items() if not ok]
        summary['BLOCKERS'] = list(summary['BLOCKERS'] or []) + [f'CHANGED_SINCE_PREFLIGHT: {", ".join(failing)}']
    summary['age_seconds'] = int(time.monotonic() - at)
    return summary


@app.get('/api/telephony/preflight')
def telephony_preflight(request: Request):
    destination = request.query_params.get('destination')
    if destination is not None and not E164.match(destination):
        raise HTTPException(422, 'destination_must_be_e164')
    return _run_preflight(destination, refresh=request.query_params.get('refresh') == '1')


@app.get('/api/status')
def public_status():
    """Cheap status for the web client; live readiness comes from the last full preflight."""
    cfg = settings.resolve()
    pack = voice_pack()
    fish_ready = _fish_ready()
    store_ok = store.healthy()
    live_checks = {'voice_pack_valid': pack.valid, 'fish_speech_ready': fish_ready, 'call_state_store_ready': store_ok,
                   'configuration_valid': not cfg.errors, 'agi_server_listening': agi_server.listening}
    readiness = _cached_readiness(live_checks) if cfg.live else None
    if cfg.live:
        with _preflight_cache_lock:
            age = time.monotonic() - _preflight_cache['at']
        if readiness is None or age > _PREFLIGHT_REFRESH_AFTER:
            _refresh_preflight_in_background()
    admin_enabled, _ = _admin_status()
    return {'mode': 'live' if cfg.live else 'demo',
            'telephony': cfg.call_provider,
            'voice': 'fish-speech-voice-pack' if pack.valid else 'voice_pack_invalid',
            'voice_pack_valid': pack.valid,
            'fish_speech_ready': fish_ready,
            'booking_repository_ready': settings.mock_mode,
            'call_state_store_ready': store_ok,
            'admin_enabled': admin_enabled,
            'live_configuration_errors': len(cfg.errors),
            'readiness': readiness,
            'clinic_phone': CLINIC['phone']}


# Outbound calls --------------------------------------------------------------------
@app.post('/api/calls/request')
def call_request(r:CallRequest):
    if not r.consent:
        raise HTTPException(400, 'consent_required')
    destination = _normalize_e164(r.patient_contact)
    if not destination:
        raise HTTPException(422, 'patient_contact_must_be_a_valid_phone_number')
    cfg = settings.resolve()
    if cfg.mock_mode:
        item = store.create_call_request(name=r.name, destination=destination, preferred_window=r.preferred_window, topic=r.topic, mode='demo')
        item = store.update_call_request(item['request_id'], status='queued')
        repo.log('call_request_created', {'request_id': item['request_id'], 'mode': 'demo'})
        return {'request_id': item['request_id'], 'status': 'queued', 'message': 'Your call request has been received.', 'mode': 'demo'}
    if cfg.errors or not cfg.asterisk_enabled:
        raise HTTPException(503, {'error': 'live_configuration_invalid', 'errors': cfg.errors})
    if destination not in cfg.allowed_destinations:
        raise HTTPException(403, {'error': 'destination_not_allowed'})
    with _call_lock:
        if store.recent_live_calls(destination, settings.outbound_rate_limit_seconds):
            raise HTTPException(429, {'error': 'call_rate_limited', 'retry_after_seconds': settings.outbound_rate_limit_seconds})
        item = store.create_call_request(name=r.name, destination=destination, preferred_window=r.preferred_window, topic=r.topic, mode='live')
    request_id = item['request_id']
    preflight = _telephony_preflight(destination, refresh=True)
    if not preflight['LIVE_CALL_ALLOWED']:
        store.update_call_request(request_id, status='blocked', error='; '.join(preflight['BLOCKERS'])[:2000])
        store.add_event(request_id, 'call_blocked', {'blockers': preflight['BLOCKERS']})
        logger.warning('live_call_blocked request_id=%s blockers=%s', request_id, preflight['BLOCKERS'])
        raise HTTPException(503, {'error': 'live_call_preflight_failed', 'request_id': request_id, 'blockers': preflight['BLOCKERS']})
    store.update_call_request(request_id, status='creating')
    try:
        result = provider.create_outbound_call(request_id, destination)
    except TelephonyError as exc:
        info = exc.as_dict()
        logger.error('call_create_failed stage=%s correlation_id=%s code=%s message=%s to=%s',
                     info['stage'], request_id, info['code'], info['message'], mask_phone(destination))
        store.update_call_request(request_id, status='failed', error=str(info)[:2000])
        store.add_event(request_id, 'call_create_failed', {**info, 'to': mask_phone(destination)})
        raise HTTPException(502, {'error': 'call_create_failed', 'stage': info['stage'], 'request_id': request_id,
                                  'provider_message': info['message']}) from exc
    store.update_call_request(request_id, call_id=result['call_id'], status=result['status'])
    store.add_event(request_id, 'call_originated', {'channel': result.get('channel'), 'provider_message': result.get('provider_message')})
    logger.info('call_originated request_id=%s status=%s to=%s', request_id, result['status'], mask_phone(destination))
    return {'request_id': request_id, 'call_id': result['call_id'], 'status': result['status'], 'mode': 'live',
            'to': destination, 'message': 'Your call request has been received.'}


@app.get('/api/calls/{request_id}')
def call_request_status(request_id:str):
    item = provider.get_call_status(request_id)
    if not item:
        raise HTTPException(404, 'call_request_not_found')
    return {k: item[k] for k in ('request_id', 'status', 'preferred_window', 'mode', 'call_id')}


@app.get('/api/calls/{request_id}/events')
def call_request_events(request_id: str, authorization: str|None=Header(default=None)):
    _require_admin(authorization)
    item = store.get_call_request(request_id)
    if not item:
        raise HTTPException(404, 'call_request_not_found')
    return {'request': {k: v for k, v in item.items() if k != 'destination'} | {'destination': mask_phone(item['destination'])},
            'events': store.events(request_id, limit=500)}


@app.post('/api/calls/{request_id}/hangup')
def call_request_hangup(request_id: str, authorization: str|None=Header(default=None)):
    _require_admin(authorization)
    if not store.get_call_request(request_id):
        raise HTTPException(404, 'call_request_not_found')
    return {'request_id': request_id, 'hangup_sent': provider.hangup_call(request_id)}


# Admin -----------------------------------------------------------------------------
@app.post('/api/admin/login')
def admin_login(r:AdminLoginRequest):
    enabled, reason = _admin_status()
    if not enabled:
        raise HTTPException(503, {'error': 'admin_disabled', 'reason': reason})
    email = settings.admin_initial_email or ('demo@example.test' if settings.mock_mode else '')
    if settings.mock_mode and not settings.admin_initial_password:
        password_ok = verify_password(r.password, _demo_admin_hash)
    else:
        password_ok = bool(settings.admin_initial_password) and hmac.compare_digest(r.password.encode(), settings.admin_initial_password.encode())
    if not email or not hmac.compare_digest(r.email.encode(), email.encode()) or not password_ok:
        raise HTTPException(401,'invalid_credentials')
    repo.log('admin_login',{'role':'admin'}); return {'access_token':_session_token(),'token_type':'bearer','role':'admin'}
@app.get('/api/usage')
def usage():
 pct=repo.usage['minutes']/3000*100; return {**repo.usage,'envelope_minutes':3000,'percent_used':round(pct,2),'threshold':'100%' if pct>=100 else '95%' if pct>=95 else '85%' if pct>=85 else '70%' if pct>=70 else 'below_70%'}
@app.get('/api/admin/config')
def get_config(authorization: str|None=Header(default=None)):
 _require_admin(authorization)
 return {'clinic':CLINIC,'services':SERVICES,'configuration_gaps':['live scheduling source','approved provider roster','pricing','consent wording','notification provider'],'roles':['receptionist','manager','admin','developer']}
@app.put('/api/admin/config')
def put_config(authorization: str|None=Header(default=None)):
 _require_admin(authorization)
 return get_config(authorization)
@app.get('/api/admin/activity')
def activity(authorization: str|None=Header(default=None)):
 _require_admin(authorization)
 return {'events':store.events(limit=100),'callbacks':store.callbacks(),'appointment_requests':store.appointment_requests(),'bookings':[{ 'reference':k,'status':v.status.value} for k,v in repo.bookings.items()]}
