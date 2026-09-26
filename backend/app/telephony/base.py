"""Telephony provider interface used by the API layer."""
from __future__ import annotations

from typing import Protocol


class TelephonyError(RuntimeError):
    """A provider failure with a stage and a safe, secret-free message."""

    def __init__(self, stage: str, message: str, *, code: str | None = None):
        super().__init__(f'{stage}: {message}')
        self.stage, self.message, self.code = stage, message, code

    def as_dict(self) -> dict:
        return {'stage': self.stage, 'message': self.message, 'code': self.code}


class TelephonyProvider(Protocol):
    name: str

    def preflight(self, destination: str | None, *, refresh: bool = False) -> dict:
        """Provider/infrastructure checks plus software and interface blockers."""

    def create_outbound_call(self, call_id: str, destination: str) -> dict:
        """Originate a real call; returns {'call_id', 'status', ...} or raises TelephonyError."""

    def get_call_status(self, call_id: str) -> dict | None:
        ...

    def hangup_call(self, call_id: str) -> bool:
        ...

    def handle_call_event(self, event: dict) -> None:
        """Apply a provider event (ringing, answered, hangup cause...) to the call record."""
