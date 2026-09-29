"""Unified call lifecycle, independent of the telephony implementation.

    REQUEST_ACCEPTED -> ORIGINATE_ACCEPTED -> CHANNEL_CREATED -> RINGING -> ANSWERED -> MEDIA_ACTIVE
        -> ENDING -> COMPLETED
    failure states: FAILED, BUSY, NO_ANSWER, TIMEOUT, CANCELLED

Each state is set only from evidence of that state:

* ``REQUEST_ACCEPTED``   - the application validated the destination and reserved the call.
* ``ORIGINATE_ACCEPTED`` - the active telephony provider accepted the originate request.
* ``CHANNEL_CREATED``    - the provider returned/reported the outbound channel object.
* ``ORIGINATE_ACCEPTED`` - the active telephony provider accepted the originate request.
* ``CHANNEL_CREATED``    - the provider returned/reported the outbound channel object.
* ``RINGING``            - the channel state became ``Ringing`` (the gateway signalled 180/183).
* ``ANSWERED``           - the far end answered (channel ``Up`` / ``StasisStart``). Never inferred
  from a successful originate.
* ``MEDIA_ACTIVE``       - the provider started playing receptionist audio on the answered channel.
* ``ENDING``             - the application asked the provider to hang up.
* ``MEDIA_ACTIVE``       - the provider started playing receptionist audio on the answered channel.
* ``ENDING``             - the application asked the provider to hang up.

Transitions only move forward (events can arrive late, duplicated or out of order) and a
terminal state is final. A state may be skipped (a gateway that sends no ringing indication goes
straight from ``CHANNEL_CREATED`` to ``ANSWERED``).
"""
from __future__ import annotations

from enum import Enum


class CallStatus(str, Enum):
    REQUEST_ACCEPTED = 'REQUEST_ACCEPTED'
    ORIGINATE_ACCEPTED = 'ORIGINATE_ACCEPTED'
    CHANNEL_CREATED = 'CHANNEL_CREATED'
    RINGING = 'RINGING'
    ANSWERED = 'ANSWERED'
    MEDIA_ACTIVE = 'MEDIA_ACTIVE'
    ENDING = 'ENDING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    BUSY = 'BUSY'
    NO_ANSWER = 'NO_ANSWER'
    TIMEOUT = 'TIMEOUT'
    CANCELLED = 'CANCELLED'


_RANK = {CallStatus.REQUEST_ACCEPTED: 0, CallStatus.ORIGINATE_ACCEPTED: 1, CallStatus.CHANNEL_CREATED: 2,
         CallStatus.RINGING: 3, CallStatus.ANSWERED: 4, CallStatus.MEDIA_ACTIVE: 5, CallStatus.ENDING: 6}
TERMINAL = frozenset({CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.BUSY, CallStatus.NO_ANSWER,
                      CallStatus.TIMEOUT, CallStatus.CANCELLED})
ACTIVE_STATES = frozenset(_RANK)
ANSWERED_STATES = frozenset({CallStatus.ANSWERED, CallStatus.MEDIA_ACTIVE, CallStatus.ENDING})


def is_terminal(status: str | CallStatus) -> bool:
    return CallStatus(status) in TERMINAL


def was_answered(status: str | CallStatus, answer_time: object = None) -> bool:
    """True when the call reached the far end (answered now, or answered before it ended)."""
    return CallStatus(status) in ANSWERED_STATES or answer_time is not None


def can_transition(current: str | CallStatus, new: str | CallStatus) -> bool:
    current, new = CallStatus(current), CallStatus(new)
    if current == new or current in TERMINAL:
        return False
    if new in TERMINAL:
        return True
    return _RANK[new] > _RANK[current]
