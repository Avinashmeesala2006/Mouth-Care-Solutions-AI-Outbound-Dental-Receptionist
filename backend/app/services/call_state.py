"""Unified call lifecycle, independent of the telephony provider.

    CREATED -> DIALING -> RINGING -> ANSWERED -> CONNECTED -> ACTIVE -> ENDING -> COMPLETED
    failure states: FAILED, BUSY, NO_ANSWER, TIMEOUT, CANCELLED

Transitions only move forward (webhooks can arrive late, duplicated or out of order), and a
terminal state is final. ``ENDING`` means the application asked for the call to end. With Twilio
TwiML a call goes ANSWERED -> ENDING/COMPLETED; ``CONNECTED``/``ACTIVE`` are kept for streamed-media
sessions and remain valid lifecycle states in the database schema.
"""
from __future__ import annotations

from enum import Enum


class CallStatus(str, Enum):
    CREATED = 'CREATED'
    DIALING = 'DIALING'
    RINGING = 'RINGING'
    ANSWERED = 'ANSWERED'
    CONNECTED = 'CONNECTED'
    ACTIVE = 'ACTIVE'
    ENDING = 'ENDING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    BUSY = 'BUSY'
    NO_ANSWER = 'NO_ANSWER'
    TIMEOUT = 'TIMEOUT'
    CANCELLED = 'CANCELLED'


_RANK = {CallStatus.CREATED: 0, CallStatus.DIALING: 1, CallStatus.RINGING: 2, CallStatus.ANSWERED: 3,
         CallStatus.CONNECTED: 4, CallStatus.ACTIVE: 5, CallStatus.ENDING: 6}
TERMINAL = frozenset({CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.BUSY, CallStatus.NO_ANSWER,
                      CallStatus.TIMEOUT, CallStatus.CANCELLED})
ACTIVE_STATES = frozenset(_RANK)
ANSWERED_STATES = frozenset({CallStatus.ANSWERED, CallStatus.CONNECTED, CallStatus.ACTIVE, CallStatus.ENDING})


def is_terminal(status: str | CallStatus) -> bool:
    return CallStatus(status) in TERMINAL


def can_transition(current: str | CallStatus, new: str | CallStatus) -> bool:
    current, new = CallStatus(current), CallStatus(new)
    if current == new or current in TERMINAL:
        return False
    if new in TERMINAL:
        return True
    return _RANK[new] > _RANK[current]
