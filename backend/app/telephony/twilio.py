"""Twilio Voice integration for the Fish Speech receptionist.

* REST (``api.twilio.com`` / ``voice.twilio.com``) with HTTP Basic auth (Account SID + Auth Token)
  from the local environment. Create-call is sent exactly once (a retry can ring twice).
* Account inventory drives readiness: account status/type, owned numbers and their voice
  capability, verified caller IDs, destination verification (Trial) and the country's voice
  dialing permission.
* TwiML webhooks play only packaged, verified Fish Speech WAV assets (``<Play>``); ``<Say>`` is
  never emitted. Caller speech is recognised by ``<Gather input="speech">`` and answered by the
  grounded receptionist engine.

Trial policy (as required for this project): a Trial account may only use Twilio's own Trial
calling mechanisms, so ``CUSTOM_WEBHOOK_CALL_ALLOWED`` is true only for an active **Full**
account. ``TRIAL_CALL_ALLOWED`` is reported separately. These readiness values are diagnostic:
an outbound request is not refused locally because of them; Twilio's own answer decides.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.config import Settings, mask_id, mask_phone, normalize_e164
from ..db.base import CallPolicy, CallRecord, CallRepository
from ..services.call_state import ANSWERED_STATES, TERMINAL, CallStatus

logger = logging.getLogger(__name__)
API = 'https://api.twilio.com/2010-04-01'
VOICE_API = 'https://voice.twilio.com/v1'
NGROK_HEADERS = {'ngrok-skip-browser-warning': 'true', 'Accept': 'application/json'}
# E.164 prefix -> ISO country (longest prefix wins). Used for Twilio voice dialing permissions.
COUNTRY_PREFIXES = {'91': 'IN', '1': 'US', '44': 'GB', '61': 'AU', '971': 'AE', '65': 'SG', '966': 'SA', '974': 'QA',
                    '49': 'DE', '33': 'FR', '64': 'NZ', '27': 'ZA', '880': 'BD', '94': 'LK', '977': 'NP'}
TWILIO_STATUS = {'queued': CallStatus.ORIGINATE_ACCEPTED, 'initiated': CallStatus.ORIGINATE_ACCEPTED,
                 'ringing': CallStatus.RINGING,
                 'in-progress': CallStatus.ANSWERED, 'answered': CallStatus.ANSWERED, 'completed': CallStatus.COMPLETED,
                 'busy': CallStatus.BUSY, 'no-answer': CallStatus.NO_ANSWER, 'failed': CallStatus.FAILED,
                 'canceled': CallStatus.CANCELLED}


class TwilioError(RuntimeError):
    def __init__(self, status_code: int, error: str, **details):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.details = details


@dataclass(frozen=True)
class TwilioCall:
    sid: str
    status: str


def twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth token, full URL + sorted key+value pairs))."""
    payload = url + ''.join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(url: str, params: dict[str, str], signature: str | None, auth_token: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(url, params, auth_token), signature)


_PHONE_IN_TEXT = re.compile(r'\+?\d{8,15}')
_ACCOUNT_SID_IN_TEXT = re.compile(r'\b(AC|SK)[0-9a-fA-F]{32}\b')


def sanitize_provider_message(text: object) -> str:
    """Provider error text safe for logs and API responses: phone numbers masked, account/key SIDs redacted."""
    value = _ACCOUNT_SID_IN_TEXT.sub(lambda m: m.group(1) + '...', str(text or ''))
    return _PHONE_IN_TEXT.sub(lambda m: mask_phone(m.group(0)), value)[:300]


def country_of(e164: str | None) -> str | None:
    digits = (e164 or '').lstrip('+')
    for length in (3, 2, 1):
        if digits[:length] in COUNTRY_PREFIXES:
            return COUNTRY_PREFIXES[digits[:length]]
    return None


class TwilioRestClient:
    """Minimal Twilio REST client. Messages never include credentials or tokens."""

    def __init__(self, account_sid: str, auth_token: str, *, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0), transport=self.transport,
                                 auth=(self.account_sid, self.auth_token), headers={'Accept': 'application/json'})

    async def _get(self, url: str) -> httpx.Response:
        async with self._client() as client:
            return await client.get(url)

    async def account(self) -> dict:
        response = await self._get(f'{API}/Accounts/{self.account_sid}.json')
        if response.status_code != 200:
            return {'authenticated': False, 'http_status': response.status_code}
        body = response.json()
        return {'authenticated': True, 'status': body.get('status'), 'type': body.get('type')}

    async def _pages(self, url: str, key: str) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        while next_url:
            response = await self._get(next_url)
            if response.status_code != 200:
                raise TwilioError(502, f'twilio_inventory_failed_{key}', http_status=response.status_code)
            body = response.json()
            items.extend(body.get(key) or [])
            next_uri = body.get('next_page_uri')
            next_url = f'https://api.twilio.com{next_uri}' if next_uri else None
        return items

    async def incoming_numbers(self) -> list[dict]:
        return await self._pages(f'{API}/Accounts/{self.account_sid}/IncomingPhoneNumbers.json?PageSize=1000',
                                 'incoming_phone_numbers')

    async def outgoing_caller_ids(self) -> list[dict]:
        return await self._pages(f'{API}/Accounts/{self.account_sid}/OutgoingCallerIds.json?PageSize=1000',
                                 'outgoing_caller_ids')

    async def dialing_permission(self, iso_code: str) -> bool | None:
        response = await self._get(f'{VOICE_API}/DialingPermissions/Countries/{iso_code}')
        if response.status_code != 200:
            return None
        return bool(response.json().get('low_risk_numbers_enabled'))

    async def create_call(self, *, to: str, from_number: str, url: str, status_callback: str) -> TwilioCall:
        # A list value is sent as repeated form keys (StatusCallbackEvent=initiated&StatusCallbackEvent=ringing...).
        data = {'To': to, 'From': from_number, 'Url': url, 'Method': 'POST', 'StatusCallback': status_callback,
                'StatusCallbackMethod': 'POST', 'StatusCallbackEvent': ['initiated', 'ringing', 'answered', 'completed']}
        stage = 'twilio_create_call'
        try:
            async with self._client() as client:
                response = await client.post(f'{API}/Accounts/{self.account_sid}/Calls.json', data=data)
        except httpx.ConnectError as exc:
            raise TwilioError(502, 'twilio_unreachable', stage=stage, code=type(exc).__name__) from exc
        except httpx.HTTPError as exc:   # sent but no answer: never retried
            raise TwilioError(502, 'twilio_outcome_unknown', stage=stage, code=type(exc).__name__) from exc
        request_id = response.headers.get('Twilio-Request-Id')
        if response.status_code not in (200, 201):
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise TwilioError(502, 'twilio_call_rejected', stage=stage, http_status=response.status_code,
                              twilio_code=body.get('code'), twilio_message=sanitize_provider_message(body.get('message')),
                              twilio_more_info=str(body.get('more_info') or '')[:200] or None,
                              twilio_request_id=request_id)
        body = response.json()
        sid = str(body.get('sid') or '')
        if not sid.startswith('CA'):
            raise TwilioError(502, 'twilio_call_missing_sid', stage=stage, twilio_request_id=request_id)
        return TwilioCall(sid=sid, status=str(body.get('status') or 'queued'))

    async def hangup(self, call_sid: str) -> bool:
        async with self._client() as client:
            response = await client.post(f'{API}/Accounts/{self.account_sid}/Calls/{call_sid}.json',
                                         data={'Status': 'completed'})
        return response.status_code in (200, 201)


class TwilioTelephony:
    def __init__(self, settings: Settings, repo: CallRepository, *, readiness, asset_url=None, client=None,
                 public_transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.repo = repo
        self.readiness = readiness            # voice-pipeline blockers (list of strings)
        self.asset_url = asset_url or (lambda _asset_id: None)
        self.client = client                  # tests inject a fake; otherwise built from settings
        self.public_transport = public_transport

    def rest(self) -> Any:
        return self.client or TwilioRestClient(self.settings.twilio_account_sid, self.settings.twilio_auth_token,
                                               timeout=self.settings.twilio_http_timeout_seconds)

    def policy(self, *, rate_limit: bool = True) -> CallPolicy:
        s = self.settings
        return CallPolicy(quota_seconds=s.quota_seconds(), max_concurrent=s.max_concurrent_app_calls,
                          reserve_seconds=s.twilio_max_call_seconds,
                          stale_after_seconds=s.twilio_max_call_seconds + 300,
                          rate_limit_seconds=s.outbound_rate_limit_seconds if rate_limit else 0)

    def configured(self) -> dict:
        s = self.settings
        cfg = s.resolve()
        return {'TWILIO_CONFIGURED': bool(s.twilio_account_sid and s.twilio_auth_token),
                'FROM_CONFIGURED': bool(cfg.from_number),
                'PUBLIC_BASE_URL_CONFIGURED': bool(cfg.public_origin)}

    def health(self) -> dict:
        s = self.settings
        flags = self.configured()
        return {'provider': 'twilio', 'enabled': s.twilio_enabled,
                'configured': flags['TWILIO_CONFIGURED'] and flags['FROM_CONFIGURED'] and flags['PUBLIC_BASE_URL_CONFIGURED'],
                'credentials_configured': flags['TWILIO_CONFIGURED'], 'from_number_configured': flags['FROM_CONFIGURED'],
                'webhook_base_url_configured': flags['PUBLIC_BASE_URL_CONFIGURED'],
                'signature_validation': bool(s.twilio_validate_signature and s.twilio_auth_token)}

    # Account inventory ----------------------------------------------------------------------------------
    async def account_check(self, destination: str | None) -> dict:
        """What the real Twilio account allows. Never fakes ownership or verification."""
        s = self.settings
        cfg = s.resolve()
        from_number = cfg.from_number or None
        result: dict[str, Any] = {
            'TWILIO_AUTHENTICATED': False, 'ACCOUNT_TYPE': 'unknown', 'ACCOUNT_STATUS': 'unknown',
            'OWNED_TWILIO_NUMBER_COUNT': None, 'FROM_CONFIGURED': bool(from_number), 'FROM_OWNED': False,
            'FROM_VOICE_CAPABLE': False, 'FROM_VERIFIED_CALLER_ID': False, 'FROM_AUTHORIZED': False,
            'DESTINATION_COUNTRY': country_of(destination), 'DESTINATION_VERIFIED_CALLER_ID': False,
            'DESTINATION_COUNTRY_PERMISSION': None, 'DESTINATION_AUTHORIZED': False, 'TRIAL_RESTRICTION': None,
            'TRIAL_CALL_ALLOWED': False, 'CUSTOM_WEBHOOK_CALL_ALLOWED': False, 'ACCOUNT_BLOCKERS': []}
        blockers: list[str] = result['ACCOUNT_BLOCKERS']
        if not (s.twilio_account_sid and s.twilio_auth_token):
            blockers.append('TWILIO_CREDENTIALS_NOT_CONFIGURED')
            return result
        rest = self.rest()
        try:
            account = await rest.account()
        except httpx.HTTPError as exc:
            blockers.append(f'TWILIO_UNREACHABLE: {type(exc).__name__}')
            return result
        if not account.get('authenticated'):
            blockers.append(f"TWILIO_AUTHENTICATION_FAILED: HTTP {account.get('http_status')}")
            return result
        result.update(TWILIO_AUTHENTICATED=True, ACCOUNT_TYPE=account.get('type') or 'unknown',
                      ACCOUNT_STATUS=account.get('status') or 'unknown')
        trial = result['ACCOUNT_TYPE'] == 'Trial'
        active = result['ACCOUNT_STATUS'] == 'active'
        result['TRIAL_RESTRICTION'] = trial
        try:
            numbers = await rest.incoming_numbers()
            caller_ids = {normalize_e164(str(c.get('phone_number') or ''), s.default_country_code)
                          for c in await rest.outgoing_caller_ids()}
            permission = (await rest.dialing_permission(result['DESTINATION_COUNTRY'])
                          if result['DESTINATION_COUNTRY'] else None)
        except (TwilioError, httpx.HTTPError) as exc:
            blockers.append(f'TWILIO_INVENTORY_UNAVAILABLE: {getattr(exc, "error", type(exc).__name__)}')
            return result
        result['OWNED_TWILIO_NUMBER_COUNT'] = len(numbers)
        owned = {normalize_e164(str(n.get('phone_number') or ''), s.default_country_code): n for n in numbers}
        if from_number in owned:
            result['FROM_OWNED'] = True
            result['FROM_VOICE_CAPABLE'] = bool((owned[from_number].get('capabilities') or {}).get('voice'))
        result['FROM_VERIFIED_CALLER_ID'] = bool(from_number and from_number in caller_ids)
        # A Full account may present an owned voice number or a verified caller ID; a Trial account only its own number.
        result['FROM_AUTHORIZED'] = bool(result['FROM_OWNED'] and result['FROM_VOICE_CAPABLE']) or bool(
            not trial and result['FROM_VERIFIED_CALLER_ID'])
        result['DESTINATION_VERIFIED_CALLER_ID'] = bool(destination and destination in caller_ids)
        result['DESTINATION_COUNTRY_PERMISSION'] = permission
        country_ok = permission is not False
        result['DESTINATION_AUTHORIZED'] = bool(destination) and country_ok and (
            result['DESTINATION_VERIFIED_CALLER_ID'] if trial else True)
        result['TRIAL_CALL_ALLOWED'] = bool(trial and active and result['FROM_AUTHORIZED'] and result['DESTINATION_AUTHORIZED'])
        result['CUSTOM_WEBHOOK_CALL_ALLOWED'] = bool(active and not trial and result['ACCOUNT_TYPE'] == 'Full')
        if not active:
            blockers.append(f"TWILIO_ACCOUNT_NOT_ACTIVE: {result['ACCOUNT_STATUS']}")
        if trial:
            blockers.append('TWILIO_TRIAL_RESTRICTION: Trial accounts may use only Twilio Trial calling mechanisms; '
                            'the application webhook call needs an upgraded (Full) account')
        if not from_number:
            blockers.append('FROM_NOT_CONFIGURED: set TWILIO_FROM_NUMBER (or OUTBOUND_FROM_NUMBER)')
        elif not result['FROM_AUTHORIZED']:
            blockers.append('FROM_NOT_AUTHORIZED: ' + ('number not owned by this account' if not result['FROM_OWNED']
                                                       else 'owned number has no voice capability'))
        if not len(numbers):
            blockers.append('NO_TWILIO_NUMBERS_OWNED')
        if destination and not result['DESTINATION_AUTHORIZED']:
            if trial and not result['DESTINATION_VERIFIED_CALLER_ID']:
                blockers.append('DESTINATION_NOT_VERIFIED: Console > Phone Numbers > Manage > Verified Caller IDs > '
                                'Add a new Caller ID for the destination')
            if permission is False:
                blockers.append(f"DESTINATION_COUNTRY_NOT_PERMITTED: enable {result['DESTINATION_COUNTRY']} in Console > "
                                'Voice > Settings > Geo permissions')
        return result

    async def probe_public(self) -> dict:
        """Reach the app through the public origin exactly as Twilio would (read-only probes)."""
        cfg = self.settings.resolve()
        result: dict[str, bool | str | None] = {
                  'PUBLIC_HEALTH': False, 'PUBLIC_INSTANCE_ID': None, 'PUBLIC_WEBHOOK_REACHABLE': False,
                  'PUBLIC_WEBHOOK_REJECTS_UNSIGNED': False, 'PUBLIC_AUDIO_READY': False}
        if not cfg.public_origin:
            return result
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self.public_transport, follow_redirects=False) as client:
                health = await client.get(f'{cfg.public_origin}/health', headers=NGROK_HEADERS)
                if health.status_code == 200 and health.headers.get('content-type', '').startswith('application/json'):
                    result['PUBLIC_HEALTH'] = True
                    result['PUBLIC_INSTANCE_ID'] = health.json().get('instance_id')
                # An unsigned webhook must reach FastAPI and be refused by signature validation.
                webhook = await client.post(cfg.status_callback_url, data={'CallStatus': 'probe'}, headers=NGROK_HEADERS)
                result['PUBLIC_WEBHOOK_REACHABLE'] = webhook.status_code in (200, 403)
                result['PUBLIC_WEBHOOK_REJECTS_UNSIGNED'] = webhook.status_code == 403
                audio_url = self.asset_url('outbound_greeting')
                if audio_url:
                    audio = await client.head(audio_url, headers={'ngrok-skip-browser-warning': 'true'})
                    result['PUBLIC_AUDIO_READY'] = (audio.status_code == 200
                                                    and audio.headers.get('content-type', '').startswith('audio/wav'))
        except (httpx.HTTPError, ValueError) as exc:
            result['PUBLIC_ERROR'] = type(exc).__name__
        return result

    # Outbound -------------------------------------------------------------------------------------------------
    async def request_outbound(self, phone_raw: str, *, source: str, requested_by: str | None,
                               idempotency_key: str | None = None, record_consent_source: str | None = None,
                               request_name: str | None = None, request_topic: str | None = None,
                               preferred_window: str | None = None) -> tuple[CallRecord, TwilioCall, bool]:
        """configuration -> phone validation -> allow-list -> consent -> reservation (consent, do-not-call,
        opt-out, quota, capacity, rate limit) -> ONE Create Call.

        The preflight report (``/api/twilio/preflight``) is not consulted here: Twilio enforces its own
        account rules (Trial restrictions, From ownership, verified destinations) and its rejection is
        recorded and returned with sanitized diagnostics.
        """
        if not self.settings.twilio_enabled:
            raise TwilioError(503, 'telephony_disabled')
        cfg = self.settings.resolve()
        if cfg.errors:
            raise TwilioError(503, 'configuration_invalid', errors=cfg.errors)
        phone = normalize_e164(phone_raw, self.settings.default_country_code)
        if not phone:
            raise TwilioError(422, 'invalid_phone_number')
        if cfg.allowed_destinations and phone not in cfg.allowed_destinations:
            raise TwilioError(403, 'destination_not_allowed')
        if record_consent_source:
            self.repo.record_consent(phone, source=record_consent_source)
        if idempotency_key:
            existing = self.repo.get_call_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, TwilioCall(existing.provider_call_id or '', existing.status), True
        reserved = self.repo.reserve_outbound_call(customer_number=phone, from_number=cfg.from_number, source=source,
                                                   requested_by=requested_by, idempotency_key=idempotency_key,
                                                   policy=self.policy(), request_name=request_name,
                                                   request_topic=request_topic, preferred_window=preferred_window)
        if reserved.blocked:
            raise TwilioError(429 if reserved.blocked in {'quota_exhausted', 'capacity_reached', 'rate_limited'} else 403,
                              reserved.blocked, **reserved.details)
        if reserved.call is None:
            raise TwilioError(500, 'reservation_failed')
        call = reserved.call
        if reserved.replayed:
            return call, TwilioCall(call.provider_call_id or '', call.status), True
        try:
            created = await self.rest().create_call(
                to=phone, from_number=cfg.from_number, url=f'{cfg.outbound_url}?session_id={call.id}',
                status_callback=f'{cfg.status_callback_url}?session_id={call.id}')
        except TwilioError as exc:
            unknown = exc.error == 'twilio_outcome_unknown'
            self.repo.transition(call.id, CallStatus.ORIGINATE_ACCEPTED if unknown else CallStatus.FAILED,
                                 reason=exc.error, error=str(exc.details.get('twilio_message') or '')[:500] or None)
            self.repo.add_event(call.id, 'twilio_create_call_failed', {'error': exc.error, **{
                k: v for k, v in exc.details.items() if k in {'stage', 'code', 'http_status', 'twilio_code', 'twilio_message',
                                                              'twilio_more_info', 'twilio_request_id'}}})
            logger.warning('twilio_create_call_failed session=%s error=%s http_status=%s twilio_code=%s request_id=%s',
                           call.id[:8], exc.error, exc.details.get('http_status'), exc.details.get('twilio_code'),
                           exc.details.get('twilio_request_id'))
            exc.details['session_id'] = call.id
            raise
        self.repo.attach_provider_call(call.id, provider_call_id=created.sid, provider_conversation_id=None)
        accepted, _ = self.repo.transition(call.id, CallStatus.ORIGINATE_ACCEPTED)
        self.repo.add_event(call.id, 'twilio_call_created', {'status': created.status})
        logger.info('twilio_call_created session=%s sid=%s to=%s', call.id[:8], mask_id(created.sid), mask_phone(phone))
        return accepted or call, created, False

    async def hangup(self, call: CallRecord) -> bool:
        if not (call.provider_call_id and self.settings.twilio_enabled):
            return False
        return await self.rest().hangup(call.provider_call_id)

    # Status callbacks --------------------------------------------------------------------------------------
    def apply_status(self, session_id: str | None, data: dict[str, str]) -> dict:
        status = (data.get('CallStatus') or '').lower()
        call_sid = data.get('CallSid') or None
        call = self.repo.get_call(session_id) if session_id else None
        if call is None and call_sid:
            call = self.repo.get_call_by_provider_id(call_sid)
        dedupe = f"twilio:{call_sid}:{status}:{data.get('SequenceNumber') or data.get('Timestamp') or ''}"
        safe = {k: data[k] for k in ('CallStatus', 'CallDuration', 'SequenceNumber', 'Timestamp', 'AnsweredBy',
                                     'SipResponseCode', 'ErrorCode', 'ErrorMessage', 'Direction') if data.get(k)}
        inserted = self.repo.add_event(call.id if call else None, 'twilio_status', safe, provider_call_id=call_sid,
                                       dedupe_key=dedupe if call_sid else None)
        if not inserted:
            return {'duplicate': True}
        if call is None:
            return {'matched': False}
        if call.provider_call_id is None and call_sid:
            self.repo.attach_provider_call(call.id, provider_call_id=call_sid, provider_conversation_id=None)
        target = TWILIO_STATUS.get(status)
        if target is None:
            return {'matched': True, 'ignored': status}
        if target == CallStatus.COMPLETED and CallStatus(call.status) not in ANSWERED_STATES \
                and CallStatus(call.status) not in TERMINAL:
            target = CallStatus.NO_ANSWER
        duration = data.get('CallDuration')
        updated, changed = self.repo.transition(call.id, target, reason=status if target in TERMINAL else None,
                                                duration_seconds=int(duration) if duration and duration.isdigit() else None)
        return {'matched': True, 'status': updated.status if updated else None, 'changed': changed}

    # TwiML -----------------------------------------------------------------------------------------------------
    def _plays(self, prompt_ids: list[str]) -> list[str] | None:
        urls = [self.asset_url(p) for p in prompt_ids]
        if not urls or any(u is None for u in urls):
            return None
        return [f'<Play>{html.escape(u)}</Play>' for u in urls if u]

    def twiml_turn(self, session_id: str, prompt_ids: list[str], *, hangup: bool) -> str:
        """Fish audio for a turn, then listen (speech can interrupt the audio) or hang up."""
        cfg = self.settings.resolve()
        plays = self._plays(prompt_ids)
        if plays is None:   # a missing verified asset: pre-generated Fish apology, then end (never another voice)
            fallback = self._plays(['technical_issue']) or []
            return '<?xml version="1.0" encoding="UTF-8"?><Response>' + ''.join(fallback) + '<Hangup/></Response>'
        if hangup:
            return '<?xml version="1.0" encoding="UTF-8"?><Response>' + ''.join(plays) + '<Hangup/></Response>'
        action = html.escape(f'{cfg.gather_url}?session_id={session_id}', quote=True)
        return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
                f'<Gather input="speech dtmf" method="POST" action="{action}" speechTimeout="auto" timeout="6" '
                f'language="en-IN" actionOnEmptyResult="true">' + ''.join(plays) + '</Gather>'
                f'<Redirect method="POST">{action}</Redirect></Response>')
