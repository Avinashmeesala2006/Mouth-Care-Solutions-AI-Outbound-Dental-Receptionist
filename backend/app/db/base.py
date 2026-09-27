"""Persistence contract shared by the PostgreSQL repository and the development/test
in-memory repository. Production always uses PostgreSQL (see ``core.config``)."""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ..services.call_state import CallStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_request_id(prefix: str) -> str:
    return f'{prefix}-{secrets.token_hex(4).upper()}'


@dataclass
class CallRecord:
    id: str
    direction: str
    customer_number: str
    status: str
    source: str
    session_mode: str = 'conversation'
    from_number: str | None = None
    provider_call_id: str | None = None
    provider_conversation_id: str | None = None
    termination_reason: str | None = None
    requested_by: str | None = None
    idempotency_key: str | None = None
    request_name: str | None = None
    request_topic: str | None = None
    preferred_window: str | None = None
    reserved_seconds: int = 0
    duration_seconds: int | None = None
    price: str | None = None
    rate: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    call_start_time: datetime | None = None
    answer_time: datetime | None = None
    connected_time: datetime | None = None
    end_time: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data


@dataclass(frozen=True)
class QuotaUsage:
    quota_seconds: int
    consumed_seconds: int
    reserved_seconds: int
    active_calls: int
    max_concurrent_calls: int

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.quota_seconds - self.consumed_seconds - self.reserved_seconds)

    def as_dict(self) -> dict[str, Any]:
        used = self.consumed_seconds + self.reserved_seconds
        return {'quota_seconds': self.quota_seconds, 'quota_hours': round(self.quota_seconds / 3600, 2),
                'consumed_seconds': self.consumed_seconds, 'reserved_seconds': self.reserved_seconds,
                'remaining_seconds': self.remaining_seconds, 'active_calls': self.active_calls,
                'max_concurrent_calls': self.max_concurrent_calls,
                'percent_used': round(100 * used / self.quota_seconds, 2) if self.quota_seconds else 100.0}


@dataclass(frozen=True)
class CallPolicy:
    """Limits applied atomically when a call is reserved."""
    quota_seconds: int
    max_concurrent: int
    reserve_seconds: int
    stale_after_seconds: int
    rate_limit_seconds: int = 0


@dataclass(frozen=True)
class ReserveResult:
    call: CallRecord | None
    blocked: str | None = None          # consent_missing | do_not_call | opted_out | quota_exhausted | ...
    replayed: bool = False              # idempotency key matched an existing call
    details: dict[str, Any] = field(default_factory=dict)


class CallRepository(Protocol):
    backend: str

    def healthy(self) -> bool: ...
    def migration_status(self) -> dict: ...
    def close(self) -> None: ...

    # Calls
    def reserve_outbound_call(self, *, customer_number: str, from_number: str, source: str, requested_by: str | None,
                              idempotency_key: str | None, policy: CallPolicy, request_name: str | None = None,
                              request_topic: str | None = None, preferred_window: str | None = None,
                              session_mode: str = 'conversation') -> ReserveResult: ...
    def create_inbound_call(self, *, provider_call_id: str, provider_conversation_id: str | None, customer_number: str,
                            from_number: str | None, policy: CallPolicy) -> ReserveResult: ...
    def get_call(self, call_id: str) -> CallRecord | None: ...
    def get_call_by_provider_id(self, provider_call_id: str) -> CallRecord | None: ...
    def get_call_by_idempotency_key(self, idempotency_key: str) -> CallRecord | None: ...
    def get_call_by_conversation_id(self, provider_conversation_id: str) -> CallRecord | None: ...
    def attach_provider_call(self, call_id: str, *, provider_call_id: str,
                             provider_conversation_id: str | None) -> CallRecord | None: ...
    def transition(self, call_id: str, new_status: CallStatus, *, reason: str | None = None, error: str | None = None,
                   duration_seconds: int | None = None, price: str | None = None,
                   rate: str | None = None) -> tuple[CallRecord | None, bool]: ...
    def expire_stale_calls(self, stale_after_seconds: int) -> int: ...
    def recent_calls(self, limit: int = 50) -> list[CallRecord]: ...

    # Events, conversation state and turns
    def add_event(self, call_id: str | None, kind: str, data: dict | None = None, *, provider_call_id: str | None = None,
                  dedupe_key: str | None = None) -> bool: ...
    def events(self, call_id: str | None = None, limit: int = 200) -> list[dict]: ...
    def load_session(self, call_id: str) -> dict | None: ...
    def save_session(self, call_id: str, state: dict) -> None: ...
    def add_turn(self, call_id: str, turn: dict) -> None: ...
    def turns(self, call_id: str) -> list[dict]: ...

    # Requests captured by the receptionist
    def add_appointment_request(self, *, call_id: str | None, caller: str, details: dict) -> dict: ...
    def add_callback(self, *, source: str, contact: str, topic: str, preferred_window: str,
                     call_id: str | None = None) -> dict: ...
    def appointment_requests(self, limit: int = 100) -> list[dict]: ...
    def callbacks(self, limit: int = 100) -> list[dict]: ...

    # Consent, do-not-call and opt-out
    def record_consent(self, phone_number: str, *, source: str, note: str | None = None) -> dict: ...
    def revoke_consent(self, phone_number: str, *, source: str) -> None: ...
    def add_dnc(self, phone_number: str, *, reason: str, source: str) -> dict: ...
    def remove_dnc(self, phone_number: str) -> bool: ...
    def record_opt_out(self, phone_number: str, *, source: str, call_id: str | None = None) -> dict: ...
    def clear_opt_out(self, phone_number: str, *, reason: str) -> int: ...
    def compliance_status(self, phone_number: str) -> dict: ...

    # Quota
    def quota_usage(self, policy: CallPolicy) -> QuotaUsage: ...
