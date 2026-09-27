"""Conversation engine seam between the voice pipeline and the receptionist logic.

The voice session only needs ``greeting`` and ``respond``; the engine never sees audio,
WebSockets, Twilio or database connections. The existing Mouth Care Solutions engine
(``services.phone_agent``) is grounded: it answers only with approved utterances, each
identified by a prompt id that maps to a verified Fish Speech voice-pack asset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..services import phone_agent
from .tts import Utterance


@dataclass
class Reply:
    intent: str
    utterances: list[Utterance]
    hangup: bool = False
    appointment: dict | None = None
    callback_topic: str | None = None
    opt_out: bool = False
    state: dict = field(default_factory=dict)

    @property
    def prompt_ids(self) -> list[str]:
        return [u.prompt_id or 'text' for u in self.utterances]


class ConversationEngine(Protocol):
    name: str

    def new_state(self, direction: str) -> dict: ...
    def greeting(self, state: dict) -> Reply: ...
    def respond(self, state: dict, transcript: str) -> Reply: ...


class GroundedReceptionistEngine:
    name = 'grounded-receptionist'

    def __init__(self, prompts: dict, *, max_turns: int = 24):
        self.prompts = prompts
        self.max_turns = max_turns

    def _utterances(self, prompt_ids: list[str]) -> list[Utterance]:
        return [Utterance(text=self.prompts[p]['text'] if p in self.prompts else '', prompt_id=p) for p in prompt_ids]

    def new_state(self, direction: str) -> dict:
        return phone_agent.new_session(direction)

    def greeting(self, state: dict) -> Reply:
        prompt = phone_agent.greeting_prompt(state.get('direction', 'inbound'))
        return Reply('greeting', self._utterances([prompt]), state=state)

    def respond(self, state: dict, transcript: str) -> Reply:
        turn = phone_agent.respond(state, transcript, max_turns=self.max_turns)
        return Reply(turn.intent, self._utterances(turn.prompts), hangup=turn.hangup, appointment=turn.appointment,
                     callback_topic=turn.callback_topic, opt_out=turn.opt_out, state=turn.state)
