from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

def now(): return datetime.now(timezone.utc)
class BookingStatus(str, Enum): SEARCHING='SEARCHING'; SLOT_FOUND='SLOT_FOUND'; HOLD='HOLD'; PATIENT_CONFIRMATION='PATIENT_CONFIRMATION'; CONFIRMED='CONFIRMED'; EXPIRED='EXPIRED'; CANCELLED='CANCELLED'; FAILED='FAILED'; HUMAN_REQUIRED='HUMAN_REQUIRED'
@dataclass
class Slot:
    id: str; starts_at: datetime; service_id: str='general'; duration_minutes: int=30; held_by: str|None=None; hold_until: datetime|None=None; booked: bool=False
@dataclass
class Booking:
    reference: str; slot_id: str; patient: dict; status: BookingStatus; idempotency_key: str; created_at: datetime=field(default_factory=now)
@dataclass
class Hold:
    token: str; slot_id: str; session_id: str; expires_at: datetime
