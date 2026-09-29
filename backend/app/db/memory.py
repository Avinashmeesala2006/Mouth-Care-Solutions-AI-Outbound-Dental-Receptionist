"""In-memory repository for development without PostgreSQL and for unit tests.

It implements exactly the same rules as :mod:`.postgres` (the shared contract tests run
against both). It is refused in staging/production by the configuration layer: state is
lost on restart and it is never a production fallback.
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import timedelta

from ..services.call_state import ACTIVE_STATES, TERMINAL, CallStatus, can_transition
from .base import CallPolicy, CallRecord, QuotaUsage, ReserveResult, new_request_id, phone_tail, utcnow


class InMemoryCallRepository:
    backend = 'memory'

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._calls: dict[str, CallRecord] = {}
        self._events: list[dict] = []
        self._dedupe: set[str] = set()
        self._sessions: dict[str, dict] = {}
        self._turns: dict[str, list[dict]] = {}
        self._appointments: list[dict] = []
        self._callbacks: list[dict] = []
        self._consents: dict[str, dict] = {}
        self._dnc: dict[str, dict] = {}
        self._opt_outs: list[dict] = []
        self._audit: list[dict] = []
        self.usage_events: list[dict] = []
        self._event_seq = 0

    # Health ---------------------------------------------------------------------------
    def healthy(self) -> bool:
        return True

    def migration_status(self) -> dict:
        return {'applied': [], 'pending': [], 'modified': [], 'unknown': [], 'backend': 'memory'}

    def close(self) -> None:
        return None

    # Helpers --------------------------------------------------------------------------
    def _active(self) -> list[CallRecord]:
        return [c for c in self._calls.values() if CallStatus(c.status) in ACTIVE_STATES]

    def _usage(self, policy: CallPolicy) -> QuotaUsage:
        consumed = sum(c.duration_seconds or 0 for c in self._calls.values() if CallStatus(c.status) in TERMINAL)
        active = self._active()
        reserved = sum(c.reserved_seconds for c in active)
        return QuotaUsage(policy.quota_seconds, consumed, reserved, len(active), policy.max_concurrent)

    def _compliance(self, phone: str) -> dict:
        consent = self._consents.get(phone)
        return {'consent': bool(consent and consent['status'] == 'granted'),
                'do_not_call': phone in self._dnc,
                'opted_out': any(o['phone_number'] == phone and o['cleared_at'] is None for o in self._opt_outs)}

    def _capacity_block(self, policy: CallPolicy) -> tuple[str | None, dict]:
        usage = self._usage(policy)
        if usage.consumed_seconds + usage.reserved_seconds + policy.reserve_seconds > policy.quota_seconds:
            return 'quota_exhausted', {'remaining_seconds': usage.remaining_seconds}
        if usage.active_calls >= policy.max_concurrent:
            return 'capacity_reached', {'active_calls': usage.active_calls}
        return None, {}

    def _apply_transition(self, call: CallRecord, new: CallStatus, *, reason=None, error=None, duration_seconds=None,
                          price=None, rate=None) -> bool:
        now = utcnow()
        if not can_transition(call.status, new):
            # A late final event may still carry the billed duration of an already-terminal call.
            if CallStatus(call.status) in TERMINAL and new in TERMINAL and duration_seconds is not None \
                    and call.duration_seconds in (None, 0):
                call.duration_seconds, call.price, call.rate, call.updated_at = int(duration_seconds), price, rate, now
            return False
        call.status = new.value
        call.updated_at = now
        if reason:
            call.termination_reason = reason
        if error:
            call.error = error[:2000]
        if new == CallStatus.ORIGINATE_ACCEPTED and call.call_start_time is None:
            call.call_start_time = now
        if new == CallStatus.ANSWERED and call.answer_time is None:
            call.answer_time = now
        if new == CallStatus.MEDIA_ACTIVE and call.connected_time is None:
            call.connected_time = now
        if new in TERMINAL:
            call.end_time = now
            if duration_seconds is not None:
                call.duration_seconds = int(duration_seconds)
            elif call.duration_seconds is None:
                call.duration_seconds = 0
            call.price, call.rate = price or call.price, rate or call.rate
            self.usage_events.append({'call_id': call.id, 'provider': call.provider, 'duration_seconds': call.duration_seconds,
                                      'outcome': call.status})
        return True

    def _expire(self, stale_after_seconds: int) -> int:
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        expired = 0
        for call in self._active():
            if call.created_at < cutoff:
                answered = call.answer_time is not None
                self._apply_transition(call, CallStatus.TIMEOUT, reason='stale_no_final_event',
                                       duration_seconds=call.reserved_seconds if answered else 0)
                expired += 1
        return expired

    # Calls -----------------------------------------------------------------------------
    def reserve_outbound_call(self, *, customer_number, from_number, source, requested_by, idempotency_key, policy,
                              request_name=None, request_topic=None, preferred_window=None,
                              session_mode='conversation') -> ReserveResult:
        with self._lock:
            self._expire(policy.stale_after_seconds)
            if idempotency_key:
                existing = next((c for c in self._calls.values() if c.idempotency_key == idempotency_key), None)
                if existing:
                    return ReserveResult(copy.deepcopy(existing), replayed=True)
            compliance = self._compliance(customer_number)
            if not compliance['consent']:
                # An opt-out also revokes consent; report the root cause.
                return ReserveResult(None, 'opted_out' if compliance['opted_out'] else 'consent_missing')
            if compliance['do_not_call']:
                return ReserveResult(None, 'do_not_call')
            if compliance['opted_out']:
                return ReserveResult(None, 'opted_out')
            blocked, details = self._capacity_block(policy)
            if blocked:
                return ReserveResult(None, blocked, details=details)
            if policy.rate_limit_seconds > 0:
                cutoff = utcnow() - timedelta(seconds=policy.rate_limit_seconds)
                recent = [c for c in self._calls.values() if c.customer_number == customer_number and c.created_at >= cutoff
                          and (c.provider_call_id or CallStatus(c.status) in ACTIVE_STATES)]
                if recent:
                    return ReserveResult(None, 'rate_limited', details={'retry_after_seconds': policy.rate_limit_seconds})
            call = CallRecord(id=str(uuid.uuid4()), direction='outbound', customer_number=customer_number,
                              status=CallStatus.REQUEST_ACCEPTED.value, source=source, session_mode=session_mode,
                              provider='twilio',
                              from_number=from_number,
                              requested_by=requested_by, idempotency_key=idempotency_key, request_name=request_name,
                              request_topic=request_topic, preferred_window=preferred_window,
                              reserved_seconds=policy.reserve_seconds)
            self._calls[call.id] = call
            self._audit_locked('call', call.id, 'outbound_call_reserved', {'source': source, 'requested_by': requested_by})
            return ReserveResult(copy.deepcopy(call))

    def create_inbound_call(self, *, provider_call_id, provider_conversation_id, customer_number,
                            from_number, policy) -> ReserveResult:
        with self._lock:
            self._expire(policy.stale_after_seconds)
            existing = next((c for c in self._calls.values() if c.provider_call_id == provider_call_id), None)
            if existing:
                return ReserveResult(copy.deepcopy(existing), replayed=True)
            blocked, details = self._capacity_block(policy)
            now = utcnow()
            # The inbound channel exists but is not answered yet.
            call = CallRecord(id=str(uuid.uuid4()), direction='inbound', customer_number=customer_number,
                              status=CallStatus.CHANNEL_CREATED.value, source='inbound', provider='twilio',
                              from_number=from_number,
                              provider_call_id=provider_call_id, provider_conversation_id=provider_conversation_id,
                              reserved_seconds=0 if blocked else policy.reserve_seconds, call_start_time=now)
            if blocked:
                call.status, call.termination_reason, call.end_time, call.duration_seconds = \
                    CallStatus.FAILED.value, blocked, now, 0
            self._calls[call.id] = call
            return ReserveResult(copy.deepcopy(call), blocked, details=details)

    def get_call(self, call_id):
        with self._lock:
            call = self._calls.get(str(call_id))
            return copy.deepcopy(call) if call else None

    def get_call_by_idempotency_key(self, idempotency_key):
        with self._lock:
            call = next((c for c in self._calls.values() if idempotency_key and c.idempotency_key == idempotency_key), None)
            return copy.deepcopy(call) if call else None

    def get_call_by_provider_id(self, provider_call_id):
        with self._lock:
            call = next((c for c in self._calls.values() if c.provider_call_id == provider_call_id), None)
            return copy.deepcopy(call) if call else None

    def get_call_by_conversation_id(self, provider_conversation_id):
        with self._lock:
            call = next((c for c in self._calls.values()
                         if provider_conversation_id and c.provider_conversation_id == provider_conversation_id), None)
            return copy.deepcopy(call) if call else None

    def attach_provider_call(self, call_id, *, provider_call_id, provider_conversation_id, channel_name=None):
        with self._lock:
            call = self._calls.get(str(call_id))
            if not call:
                return None
            if call.provider_call_id in (None, provider_call_id):
                call.provider_call_id = provider_call_id
                call.provider_conversation_id = provider_conversation_id or call.provider_conversation_id
                call.channel_name = channel_name or call.channel_name
                call.updated_at = utcnow()
            return copy.deepcopy(call)

    def transition(self, call_id, new_status, *, reason=None, error=None, duration_seconds=None, price=None, rate=None):
        with self._lock:
            call = self._calls.get(str(call_id))
            if not call:
                return None, False
            changed = self._apply_transition(call, CallStatus(new_status), reason=reason, error=error,
                                             duration_seconds=duration_seconds, price=price, rate=rate)
            return copy.deepcopy(call), changed

    def expire_stale_calls(self, stale_after_seconds):
        with self._lock:
            return self._expire(stale_after_seconds)

    def recent_calls(self, limit=50):
        with self._lock:
            return [copy.deepcopy(c) for c in sorted(self._calls.values(), key=lambda c: c.created_at, reverse=True)[:limit]]

    # Events, sessions, turns -------------------------------------------------------------
    def add_event(self, call_id, kind, data=None, *, provider_call_id=None, dedupe_key=None) -> bool:
        with self._lock:
            if dedupe_key:
                if dedupe_key in self._dedupe:
                    return False
                self._dedupe.add(dedupe_key)
            self._event_seq += 1
            self._events.append({'id': self._event_seq, 'call_id': str(call_id) if call_id else None,
                                 'provider_call_id': provider_call_id,
                                 'kind': kind, 'data': copy.deepcopy(data or {}), 'created_at': utcnow().isoformat()})
            return True

    def events(self, call_id=None, limit=200):
        with self._lock:
            rows = [e for e in self._events if call_id is None or e['call_id'] == str(call_id)]
            return copy.deepcopy(rows[-limit:])

    def load_session(self, call_id):
        with self._lock:
            state = self._sessions.get(str(call_id))
            return copy.deepcopy(state) if state is not None else None

    def save_session(self, call_id, state):
        with self._lock:
            self._sessions[str(call_id)] = copy.deepcopy(state)

    def add_turn(self, call_id, turn):
        with self._lock:
            self._turns.setdefault(str(call_id), []).append(copy.deepcopy(turn))

    def turns(self, call_id):
        with self._lock:
            return copy.deepcopy(self._turns.get(str(call_id), []))

    # Captured requests ---------------------------------------------------------------------
    def add_appointment_request(self, *, call_id, caller, details, idempotency_key=None):
        now = utcnow().isoformat()
        with self._lock:
            if idempotency_key:
                existing = next((r for r in self._appointments if r['idempotency_key'] == idempotency_key), None)
                if existing:
                    return {**existing, 'replayed': True}
            record = {'id': new_request_id('AR'), 'call_id': str(call_id) if call_id else None, 'caller': caller,
                      'patient_name': details.get('patient_name', ''), 'preferred_date': details.get('preferred_date', ''),
                      'preferred_time': details.get('preferred_time', ''), 'reason': details.get('reason', ''),
                      'status': 'requested', 'idempotency_key': idempotency_key, 'confirmed_at': now,
                      'created_at': now, 'updated_at': now}
            self._appointments.append(record)
            self._audit_locked('appointment_request', record['id'], 'appointment_request_created',
                               {'call_id': record['call_id'], 'status': 'requested'})
            return {**record, 'replayed': False}

    def add_callback(self, *, source, contact, topic, preferred_window, call_id=None):
        record = {'id': new_request_id('CB'), 'source': source, 'call_id': str(call_id) if call_id else None, 'contact': contact,
                  'topic': topic, 'preferred_window': preferred_window, 'status': 'queued', 'created_at': utcnow().isoformat()}
        with self._lock:
            self._callbacks.append(record)
        return dict(record)

    def appointment_requests(self, limit=100):
        with self._lock:
            return [dict(r) for r in reversed(self._appointments[-limit:])]

    def callbacks(self, limit=100):
        with self._lock:
            return [dict(r) for r in reversed(self._callbacks[-limit:])]

    # Compliance ----------------------------------------------------------------------------
    def record_consent(self, phone_number, *, source, note=None):
        now = utcnow().isoformat()
        with self._lock:
            self._consents[phone_number] = {'phone_number': phone_number, 'status': 'granted', 'source': source,
                                            'note': note, 'granted_at': now, 'revoked_at': None}
            self._audit_locked('consent', phone_tail(phone_number), 'consent_granted', {'source': source})
            return dict(self._consents[phone_number])

    def revoke_consent(self, phone_number, *, source):
        with self._lock:
            current = self._consents.get(phone_number)
            if current:
                current.update(status='revoked', revoked_at=utcnow().isoformat(), source=source)
            else:
                self._consents[phone_number] = {'phone_number': phone_number, 'status': 'revoked', 'source': source,
                                                'note': None, 'granted_at': None, 'revoked_at': utcnow().isoformat()}
            self._audit_locked('consent', phone_tail(phone_number), 'consent_revoked', {'source': source})

    def add_dnc(self, phone_number, *, reason, source):
        with self._lock:
            self._dnc[phone_number] = {'phone_number': phone_number, 'reason': reason, 'source': source,
                                       'created_at': utcnow().isoformat()}
            self._audit_locked('do_not_call', phone_tail(phone_number), 'do_not_call_added', {'source': source})
            return dict(self._dnc[phone_number])

    def remove_dnc(self, phone_number):
        with self._lock:
            removed = self._dnc.pop(phone_number, None) is not None
            if removed:
                self._audit_locked('do_not_call', phone_tail(phone_number), 'do_not_call_removed', {})
            return removed

    def record_opt_out(self, phone_number, *, source, call_id=None):
        with self._lock:
            record = {'phone_number': phone_number, 'source': source, 'call_id': str(call_id) if call_id else None,
                      'created_at': utcnow().isoformat(), 'cleared_at': None, 'cleared_reason': None}
            self._opt_outs.append(record)
            self.revoke_consent(phone_number, source=f'opt_out:{source}')
            self._audit_locked('opt_out', phone_tail(phone_number), 'opt_out_recorded',
                               {'source': source, 'call_id': record['call_id']})
            return dict(record)

    def clear_opt_out(self, phone_number, *, reason):
        with self._lock:
            count = 0
            for record in self._opt_outs:
                if record['phone_number'] == phone_number and record['cleared_at'] is None:
                    record.update(cleared_at=utcnow().isoformat(), cleared_reason=reason)
                    count += 1
            if count:
                self._audit_locked('opt_out', phone_tail(phone_number), 'opt_out_cleared', {'reason': reason, 'rows': count})
            return count

    def compliance_status(self, phone_number):
        with self._lock:
            return self._compliance(phone_number)

    # Audit ---------------------------------------------------------------------------------
    def _audit_locked(self, entity_type: str, entity_id: str, event_type: str, payload: dict | None) -> None:
        self._audit.append({'id': str(uuid.uuid4()), 'entity_type': entity_type, 'entity_id': entity_id,
                            'event_type': event_type, 'payload': copy.deepcopy(payload or {}),
                            'created_at': utcnow().isoformat()})

    def audit(self, entity_type, entity_id, event_type, payload=None):
        with self._lock:
            self._audit_locked(entity_type, entity_id, event_type, payload)

    def audit_events(self, limit=100):
        with self._lock:
            return copy.deepcopy(list(reversed(self._audit[-limit:])))

    # Quota ---------------------------------------------------------------------------------
    def quota_usage(self, policy):
        with self._lock:
            self._expire(policy.stale_after_seconds)
            return self._usage(policy)
