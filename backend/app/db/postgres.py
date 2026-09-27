"""PostgreSQL repository (psycopg 3 + connection pool).

Call reservation (compliance, quota, concurrency, rate limit and the insert) runs in one
transaction under a transaction-scoped advisory lock, so concurrent requests can never
exceed the configured quota or concurrent-call limit. Provider events are de-duplicated
with a unique ``dedupe_key``; status changes only move forward (``call_state``).
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..services.call_state import ACTIVE_STATES, TERMINAL, CallStatus, can_transition
from .base import CallPolicy, CallRecord, QuotaUsage, ReserveResult, new_request_id, utcnow
from .migrations import status as migration_status

logger = logging.getLogger(__name__)
_CALL_LOCK = 7_401_202_610
_ACTIVE = tuple(s.value for s in ACTIVE_STATES)
_TERMINAL = tuple(s.value for s in TERMINAL)
_CALL_FIELDS = tuple(CallRecord.__dataclass_fields__)


def _record(row: dict[str, Any] | None) -> CallRecord | None:
    if not row:
        return None
    data = {k: row[k] for k in _CALL_FIELDS if k in row}
    data['id'] = str(data['id'])
    return CallRecord(**data)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else _iso(v)) for k, v in row.items()}


class PostgresCallRepository:
    backend = 'postgresql'

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10, open_timeout: float = 5.0,
                 acquire_timeout: float = 5.0):
        self._url = database_url
        self._min_size, self._max_size = min_size, max_size
        self.open_timeout, self.acquire_timeout = open_timeout, acquire_timeout
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    def open(self, wait: bool = True) -> None:
        with self._lock:
            if self._pool is not None:
                return
            pool = ConnectionPool(self._url, min_size=self._min_size, max_size=self._max_size, open=False,
                                  kwargs={'row_factory': dict_row, 'connect_timeout': 5}, timeout=self.acquire_timeout,
                                  name='mouthcare-calls')
            try:
                pool.open(wait=wait, timeout=self.open_timeout)
            except Exception:
                with contextlib.suppress(Exception):
                    pool.close()          # a failed pool cannot be reopened; the next call builds a new one
                raise
            self._pool = pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    def _conn(self):
        self.open()
        pool = self._pool
        if pool is None:
            raise RuntimeError('database pool is closed')
        return pool.connection()

    # Health ----------------------------------------------------------------------------------
    def healthy(self) -> bool:
        try:
            self.open()
            if self._pool is None:
                return False
            with self._pool.connection(timeout=3.0) as conn:
                conn.execute('SELECT 1')
            return True
        except Exception as exc:  # any driver/pool error means not healthy
            logger.warning('postgres_unhealthy error=%s', type(exc).__name__)
            return False

    def migration_status(self) -> dict:
        with self._conn() as conn:
            return migration_status(conn) | {'backend': 'postgresql'}

    # Helpers ----------------------------------------------------------------------------------
    @staticmethod
    def _usage(conn, policy: CallPolicy) -> QuotaUsage:
        row = conn.execute(
            'SELECT COALESCE(SUM(duration_seconds) FILTER (WHERE status = ANY(%(terminal)s)), 0) AS consumed, '
            'COALESCE(SUM(reserved_seconds) FILTER (WHERE status = ANY(%(active)s)), 0) AS reserved, '
            'COUNT(*) FILTER (WHERE status = ANY(%(active)s)) AS active FROM calls',
            {'terminal': list(_TERMINAL), 'active': list(_ACTIVE)}).fetchone()
        return QuotaUsage(policy.quota_seconds, int(row['consumed']), int(row['reserved']), int(row['active']),
                          policy.max_concurrent)

    @staticmethod
    def _compliance(conn, phone: str) -> dict:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM contact_consents WHERE phone_number = %(p)s AND status = 'granted') AS consent, "
            'EXISTS (SELECT 1 FROM do_not_call WHERE phone_number = %(p)s) AS do_not_call, '
            'EXISTS (SELECT 1 FROM opt_outs WHERE phone_number = %(p)s AND cleared_at IS NULL) AS opted_out',
            {'p': phone}).fetchone()
        return {'consent': row['consent'], 'do_not_call': row['do_not_call'], 'opted_out': row['opted_out']}

    def _capacity_block(self, conn, policy: CallPolicy) -> tuple[str | None, dict]:
        usage = self._usage(conn, policy)
        if usage.consumed_seconds + usage.reserved_seconds + policy.reserve_seconds > policy.quota_seconds:
            return 'quota_exhausted', {'remaining_seconds': usage.remaining_seconds}
        if usage.active_calls >= policy.max_concurrent:
            return 'capacity_reached', {'active_calls': usage.active_calls}
        return None, {}

    def _transition(self, conn, call_id: str, new: CallStatus, *, reason=None, error=None, duration_seconds=None,
                    price=None, rate=None) -> tuple[CallRecord | None, bool]:
        current = _record(conn.execute('SELECT * FROM calls WHERE id = %s FOR UPDATE', (call_id,)).fetchone())
        if current is None:
            return None, False
        if not can_transition(current.status, new):
            # A late final event may still carry the billed duration of an already-terminal call.
            if (CallStatus(current.status) in TERMINAL and new in TERMINAL and duration_seconds is not None
                    and current.duration_seconds in (None, 0)):
                row = conn.execute('UPDATE calls SET duration_seconds = %s, price = %s, rate = %s, updated_at = now() '
                                   'WHERE id = %s RETURNING *', (int(duration_seconds), price, rate, call_id)).fetchone()
                return _record(row), False
            return current, False
        sets = ['status = %(status)s', 'updated_at = now()']
        params: dict[str, Any] = {'status': new.value, 'id': call_id}
        if reason:
            sets.append('termination_reason = %(reason)s')
            params['reason'] = reason
        if error:
            sets.append('error = %(error)s')
            params['error'] = error[:2000]
        if new == CallStatus.DIALING:
            sets.append('call_start_time = COALESCE(call_start_time, now())')
        if new == CallStatus.ANSWERED:
            sets.append('answer_time = COALESCE(answer_time, now())')
        if new == CallStatus.CONNECTED:
            sets.append('connected_time = COALESCE(connected_time, now())')
        if new in TERMINAL:
            sets.append('end_time = now()')
            sets.append('duration_seconds = COALESCE(%(duration)s, duration_seconds, 0)')
            sets.append('price = COALESCE(%(price)s, price)')
            sets.append('rate = COALESCE(%(rate)s, rate)')
            params.update(duration=None if duration_seconds is None else int(duration_seconds), price=price, rate=rate)
        row = conn.execute(f'UPDATE calls SET {", ".join(sets)} WHERE id = %(id)s RETURNING *', params).fetchone()
        record = _record(row)
        if new in TERMINAL and record is not None:
            duration = record.duration_seconds or 0
            try:
                cost = float(record.price) if record.price else None
            except ValueError:
                cost = None
            conn.execute('INSERT INTO usage_events (id, channel, provider, session_type, duration_seconds, billable_minutes, '
                         'estimated_cost, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                         (uuid.uuid4(), 'phone', 'twilio', record.direction, duration, math.ceil(duration / 60), cost,
                          record.status))
        return record, True

    def _expire(self, conn, stale_after_seconds: int) -> int:
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        rows = conn.execute('SELECT id, answer_time, reserved_seconds FROM calls WHERE status = ANY(%s) AND created_at < %s',
                            (list(_ACTIVE), cutoff)).fetchall()
        for row in rows:
            answered = row['answer_time'] is not None
            self._transition(conn, str(row['id']), CallStatus.TIMEOUT, reason='stale_no_final_event',
                             duration_seconds=row['reserved_seconds'] if answered else 0)
        return len(rows)

    # Calls ------------------------------------------------------------------------------------------
    def reserve_outbound_call(self, *, customer_number, from_number, source, requested_by, idempotency_key, policy,
                              request_name=None, request_topic=None, preferred_window=None,
                              session_mode='conversation') -> ReserveResult:
        with self._conn() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (_CALL_LOCK,))
            self._expire(conn, policy.stale_after_seconds)
            if idempotency_key:
                existing = _record(conn.execute('SELECT * FROM calls WHERE idempotency_key = %s', (idempotency_key,)).fetchone())
                if existing:
                    return ReserveResult(existing, replayed=True)
            compliance = self._compliance(conn, customer_number)
            if not compliance['consent']:
                # An opt-out also revokes consent; report the root cause.
                return ReserveResult(None, 'opted_out' if compliance['opted_out'] else 'consent_missing')
            if compliance['do_not_call']:
                return ReserveResult(None, 'do_not_call')
            if compliance['opted_out']:
                return ReserveResult(None, 'opted_out')
            blocked, details = self._capacity_block(conn, policy)
            if blocked:
                return ReserveResult(None, blocked, details=details)
            if policy.rate_limit_seconds > 0:
                recent = conn.execute(
                    'SELECT COUNT(*) AS n FROM calls WHERE customer_number = %s AND created_at >= %s '
                    'AND (provider_call_id IS NOT NULL OR status = ANY(%s))',
                    (customer_number, utcnow() - timedelta(seconds=policy.rate_limit_seconds), list(_ACTIVE))).fetchone()['n']
                if recent:
                    return ReserveResult(None, 'rate_limited', details={'retry_after_seconds': policy.rate_limit_seconds})
            row = conn.execute(
                'INSERT INTO calls (id, direction, customer_number, from_number, status, source, session_mode, requested_by, '
                'idempotency_key, request_name, request_topic, preferred_window, reserved_seconds) VALUES '
                "(%s, 'outbound', %s, %s, 'CREATED', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), customer_number, from_number, source, session_mode, requested_by, idempotency_key,
                 request_name, request_topic, preferred_window, policy.reserve_seconds)).fetchone()
            return ReserveResult(_record(row))

    def create_inbound_call(self, *, provider_call_id, provider_conversation_id, customer_number,
                            from_number, policy) -> ReserveResult:
        with self._conn() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (_CALL_LOCK,))
            self._expire(conn, policy.stale_after_seconds)
            existing = _record(conn.execute('SELECT * FROM calls WHERE provider_call_id = %s', (provider_call_id,)).fetchone())
            if existing:
                return ReserveResult(existing, replayed=True)
            blocked, details = self._capacity_block(conn, policy)
            if blocked:
                row = conn.execute(
                    'INSERT INTO calls (id, direction, customer_number, from_number, status, termination_reason, source, '
                    'provider_call_id, provider_conversation_id, reserved_seconds, duration_seconds, call_start_time, '
                    "answer_time, end_time) VALUES (%s, 'inbound', %s, %s, 'FAILED', %s, 'inbound', %s, %s, 0, 0, now(), now(), "
                    'now()) RETURNING *',
                    (uuid.uuid4(), customer_number, from_number, blocked, provider_call_id, provider_conversation_id)).fetchone()
                return ReserveResult(_record(row), blocked, details=details)
            row = conn.execute(
                'INSERT INTO calls (id, direction, customer_number, from_number, status, source, provider_call_id, '
                "provider_conversation_id, reserved_seconds, call_start_time, answer_time) VALUES (%s, 'inbound', %s, %s, "
                "'ANSWERED', 'inbound', %s, %s, %s, now(), now()) RETURNING *",
                (uuid.uuid4(), customer_number, from_number, provider_call_id, provider_conversation_id,
                 policy.reserve_seconds)).fetchone()
            return ReserveResult(_record(row))

    def get_call(self, call_id):
        try:
            uuid.UUID(str(call_id))
        except ValueError:
            return None
        with self._conn() as conn:
            return _record(conn.execute('SELECT * FROM calls WHERE id = %s', (str(call_id),)).fetchone())

    def get_call_by_idempotency_key(self, idempotency_key):
        with self._conn() as conn:
            return _record(conn.execute('SELECT * FROM calls WHERE idempotency_key = %s', (idempotency_key,)).fetchone())

    def get_call_by_provider_id(self, provider_call_id):
        with self._conn() as conn:
            return _record(conn.execute('SELECT * FROM calls WHERE provider_call_id = %s', (provider_call_id,)).fetchone())

    def get_call_by_conversation_id(self, provider_conversation_id):
        if not provider_conversation_id:
            return None
        with self._conn() as conn:
            return _record(conn.execute('SELECT * FROM calls WHERE provider_conversation_id = %s ORDER BY created_at LIMIT 1',
                                        (provider_conversation_id,)).fetchone())

    def attach_provider_call(self, call_id, *, provider_call_id, provider_conversation_id):
        with self._conn() as conn, conn.transaction():
            row = conn.execute(
                'UPDATE calls SET provider_call_id = %s, provider_conversation_id = COALESCE(%s, provider_conversation_id), '
                'updated_at = now() WHERE id = %s AND (provider_call_id IS NULL OR provider_call_id = %s) RETURNING *',
                (provider_call_id, provider_conversation_id, call_id, provider_call_id)).fetchone()
            return _record(row) or _record(conn.execute('SELECT * FROM calls WHERE id = %s', (call_id,)).fetchone())

    def transition(self, call_id, new_status, *, reason=None, error=None, duration_seconds=None, price=None, rate=None):
        with self._conn() as conn, conn.transaction():
            return self._transition(conn, str(call_id), CallStatus(new_status), reason=reason, error=error,
                                    duration_seconds=duration_seconds, price=price, rate=rate)

    def expire_stale_calls(self, stale_after_seconds):
        with self._conn() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (_CALL_LOCK,))
            return self._expire(conn, stale_after_seconds)

    def recent_calls(self, limit=50):
        with self._conn() as conn:
            rows = conn.execute('SELECT * FROM calls ORDER BY created_at DESC LIMIT %s', (limit,)).fetchall()
            return [_record(r) for r in rows]

    # Events, sessions, turns -----------------------------------------------------------------------
    def add_event(self, call_id, kind, data=None, *, provider_call_id=None, dedupe_key=None) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                'INSERT INTO call_events (call_id, provider_call_id, kind, dedupe_key, data) VALUES (%s, %s, %s, %s, %s) '
                'ON CONFLICT (dedupe_key) DO NOTHING RETURNING id',
                (call_id, provider_call_id, kind, dedupe_key,
                 Jsonb(json.loads(json.dumps(data or {}, default=str))))
            ).fetchone()
            return row is not None

    def events(self, call_id=None, limit=200):
        with self._conn() as conn:
            if call_id:
                rows = conn.execute('SELECT * FROM call_events WHERE call_id = %s ORDER BY id DESC LIMIT %s',
                                    (call_id, limit)).fetchall()
            else:
                rows = conn.execute('SELECT * FROM call_events ORDER BY id DESC LIMIT %s', (limit,)).fetchall()
        return [_row(r) for r in reversed(rows)]

    def load_session(self, call_id):
        with self._conn() as conn:
            row = conn.execute('SELECT state FROM call_sessions WHERE call_id = %s', (call_id,)).fetchone()
        return row['state'] if row else None

    def save_session(self, call_id, state):
        with self._conn() as conn:
            conn.execute('INSERT INTO call_sessions (call_id, state, updated_at) VALUES (%s, %s, now()) '
                         'ON CONFLICT (call_id) DO UPDATE SET state = excluded.state, updated_at = now()',
                         (call_id, Jsonb(state)))

    def add_turn(self, call_id, turn):
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO call_turns (call_id, turn_index, transcript, intent, prompts, barge_in, stt_latency_ms, '
                'ai_latency_ms, tts_latency_ms, first_audio_latency_ms, total_turn_latency_ms) '
                'VALUES (%(call_id)s, %(turn_index)s, %(transcript)s, %(intent)s, %(prompts)s, %(barge_in)s, %(stt_latency_ms)s, '
                '%(ai_latency_ms)s, %(tts_latency_ms)s, %(first_audio_latency_ms)s, %(total_turn_latency_ms)s)',
                {'call_id': call_id, 'turn_index': turn.get('turn_index', 0), 'transcript': turn.get('transcript'),
                 'intent': turn.get('intent'), 'prompts': Jsonb(turn.get('prompts') or []),
                 'barge_in': bool(turn.get('barge_in')), **{k: turn.get(k) for k in (
                     'stt_latency_ms', 'ai_latency_ms', 'tts_latency_ms', 'first_audio_latency_ms', 'total_turn_latency_ms')}})

    def turns(self, call_id):
        with self._conn() as conn:
            rows = conn.execute('SELECT * FROM call_turns WHERE call_id = %s ORDER BY turn_index, id', (call_id,)).fetchall()
        return [_row(r) for r in rows]

    # Captured requests ------------------------------------------------------------------------------
    def add_appointment_request(self, *, call_id, caller, details):
        record = {'id': new_request_id('AR'), 'call_id': call_id, 'caller': caller,
                  'patient_name': details.get('patient_name', ''), 'preferred_date': details.get('preferred_date', ''),
                  'preferred_time': details.get('preferred_time', ''), 'reason': details.get('reason', ''),
                  'status': 'requested'}
        with self._conn() as conn, conn.transaction():
            row = conn.execute(
                'INSERT INTO appointment_requests (id, call_id, caller, patient_name, preferred_date, preferred_time, reason, '
                'status) VALUES (%(id)s, %(call_id)s, %(caller)s, %(patient_name)s, %(preferred_date)s, %(preferred_time)s, '
                '%(reason)s, %(status)s) RETURNING *', record).fetchone()
            if caller:
                conn.execute('INSERT INTO patients (id, name, phone) VALUES (%s, %s, %s) '
                             'ON CONFLICT (phone) WHERE phone IS NOT NULL '
                             'DO UPDATE SET name = COALESCE(NULLIF(excluded.name, %s), patients.name)',
                             (uuid.uuid4(), record['patient_name'] or None, caller, ''))
        return _row(row)

    def add_callback(self, *, source, contact, topic, preferred_window, call_id=None):
        with self._conn() as conn:
            row = conn.execute(
                'INSERT INTO callback_requests (id, source, call_id, contact, topic, preferred_window, status) '
                "VALUES (%s, %s, %s, %s, %s, %s, 'queued') RETURNING *",
                (new_request_id('CB'), source, call_id, contact, topic, preferred_window)).fetchone()
        return _row(row)

    def appointment_requests(self, limit=100):
        with self._conn() as conn:
            return [_row(r) for r in conn.execute('SELECT * FROM appointment_requests ORDER BY created_at DESC LIMIT %s',
                                                  (limit,)).fetchall()]

    def callbacks(self, limit=100):
        with self._conn() as conn:
            return [_row(r) for r in conn.execute('SELECT * FROM callback_requests ORDER BY created_at DESC LIMIT %s',
                                                  (limit,)).fetchall()]

    # Compliance ---------------------------------------------------------------------------------------
    def record_consent(self, phone_number, *, source, note=None):
        with self._conn() as conn:
            row = conn.execute(
                "INSERT INTO contact_consents (phone_number, status, source, note, granted_at, revoked_at, updated_at) "
                "VALUES (%s, 'granted', %s, %s, now(), NULL, now()) ON CONFLICT (phone_number) DO UPDATE SET "
                "status = 'granted', source = excluded.source, note = excluded.note, granted_at = now(), revoked_at = NULL, "
                'updated_at = now() RETURNING *', (phone_number, source, note)).fetchone()
        return _row(row)

    def revoke_consent(self, phone_number, *, source):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO contact_consents (phone_number, status, source, revoked_at, updated_at) VALUES "
                "(%s, 'revoked', %s, now(), now()) ON CONFLICT (phone_number) DO UPDATE SET status = 'revoked', "
                'source = excluded.source, revoked_at = now(), updated_at = now()', (phone_number, source))

    def add_dnc(self, phone_number, *, reason, source):
        with self._conn() as conn:
            row = conn.execute(
                'INSERT INTO do_not_call (phone_number, reason, source) VALUES (%s, %s, %s) ON CONFLICT (phone_number) '
                'DO UPDATE SET reason = excluded.reason, source = excluded.source RETURNING *',
                (phone_number, reason, source)).fetchone()
        return _row(row)

    def remove_dnc(self, phone_number):
        with self._conn() as conn:
            return conn.execute('DELETE FROM do_not_call WHERE phone_number = %s', (phone_number,)).rowcount > 0

    def record_opt_out(self, phone_number, *, source, call_id=None):
        with self._conn() as conn, conn.transaction():
            row = conn.execute('INSERT INTO opt_outs (phone_number, source, call_id) VALUES (%s, %s, %s) RETURNING *',
                               (phone_number, source, call_id)).fetchone()
            conn.execute(
                "INSERT INTO contact_consents (phone_number, status, source, revoked_at, updated_at) VALUES "
                "(%s, 'revoked', %s, now(), now()) ON CONFLICT (phone_number) DO UPDATE SET status = 'revoked', "
                'source = excluded.source, revoked_at = now(), updated_at = now()', (phone_number, f'opt_out:{source}'))
        return _row(row)

    def clear_opt_out(self, phone_number, *, reason):
        with self._conn() as conn, conn.transaction():
            count = conn.execute('UPDATE opt_outs SET cleared_at = now(), cleared_reason = %s WHERE phone_number = %s '
                                 'AND cleared_at IS NULL', (reason, phone_number)).rowcount
            if count:
                conn.execute('INSERT INTO audit_events (id, entity_type, entity_id, event_type, payload) '
                             'VALUES (%s, %s, %s, %s, %s)',
                             (uuid.uuid4(), 'opt_out', phone_number[-4:], 'opt_out_cleared',
                              Jsonb({'reason': reason, 'rows': count})))
            return count

    def compliance_status(self, phone_number):
        with self._conn() as conn:
            return self._compliance(conn, phone_number)

    # Quota -----------------------------------------------------------------------------------------------
    def quota_usage(self, policy):
        with self._conn() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (_CALL_LOCK,))
            self._expire(conn, policy.stale_after_seconds)
            return self._usage(conn, policy)
