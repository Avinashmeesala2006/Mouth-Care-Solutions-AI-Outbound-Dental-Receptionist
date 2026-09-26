"""Grounded phone receptionist conversation logic.

Every reply is a sequence of packaged voice assets (see knowledge/clinic/voice_prompts.json),
so the caller only ever hears approved Mouth Care Solutions wording in the Fish
reference voice. The state machine is pure: it takes the stored session state and
the caller's recognized speech and returns the prompts to play plus any request
(appointment or callback) that must be persisted. Nothing here diagnoses, quotes
prices, names doctors or confirms bookings: appointment requests are recorded for
the clinic team to confirm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SILENT_TURNS = 2
MAX_UNCLEAR_TURNS = 3
MAX_FIELD_CHARS = 200


def _pattern(*phrases: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b", re.IGNORECASE)


EMERGENCY = _pattern(r"emergenc(?:y|ies)", r"urgent(?:ly)?", r"bleed(?:ing|s)?", r"swell(?:ing|ed)?", r"swollen",
                     r"severe(?:ly)? pain", r"(?:unbearable|terrible|extreme|a lot of) pain", r"knocked out",
                     r"broken (?:tooth|teeth|jaw)", r"accident", r"injur(?:y|ed|ies)", r"can'?t breathe", r"trauma")
UNSUPPORTED = _pattern(r"prices?", r"pricing", r"costs?", r"how much", r"fees?", r"charges?", r"rupees", r"insurance",
                       r"insured", r"discounts?", r"payment", r"emi", r"which doctor", r"doctor'?s name",
                       r"dentist'?s name", r"who is the (?:doctor|dentist)", r"qualifications?", r"holidays?",
                       r"guarantee(?:d|s)?", r"diagnos(?:e|is)", r"prescri(?:be|ption)", r"medicines?", r"painkillers?")
HUMAN = _pattern(r"human", r"real person", r"a person", r"someone", r"staff", r"speak to", r"talk to",
                 r"call me back", r"call back", r"callback", r"manager")
APPOINTMENT = _pattern(r"appointments?", r"book(?:ing)?", r"schedul(?:e|ing)", r"reschedul(?:e|ing)", r"slots?",
                       r"consultation", r"check ?ups?", r"see (?:a|the) (?:dentist|doctor)", r"come in", r"visit")
HOURS = _pattern(r"hours?", r"timings?", r"open(?:ing)?", r"clos(?:e|ed|ing)", r"what time", r"when are you",
                 r"working days?", r"sundays?", r"saturdays?")
ADDRESS = _pattern(r"address", r"located", r"location", r"where are you", r"where is", r"directions?",
                   r"how (?:do|can) i (?:get|reach)", r"landmark")
PHONE = _pattern(r"phone", r"telephone", r"contact number", r"your number", r"mobile number", r"call you")
EMAIL = _pattern(r"e-?mail", r"mail id")
SERVICES = _pattern(r"services?", r"treatments?", r"provide", r"offer", r"root canal", r"braces", r"aligners?",
                    r"implants?", r"cleaning", r"whitening", r"fillings?", r"extractions?", r"dentures?",
                    r"crowns?", r"kids", r"children", r"pediatric", r"cosmetic", r"orthodontic")
END = _pattern(r"bye", r"good ?bye", r"that'?s all", r"that is all", r"nothing else", r"no thanks?",
               r"no thank you", r"hang up", r"i'?m done", r"that'?s it")
THANKS = _pattern(r"thanks?", r"thank you")
AFFIRM = _pattern(r"yes", r"yeah", r"yep", r"yup", r"sure", r"correct", r"confirm(?:ed)?", r"please do",
                  r"go ahead", r"okay", r"ok", r"right", r"absolutely", r"of course", r"send it")
NEGATE = _pattern(r"no", r"nope", r"nah", r"don'?t", r"do not", r"not now", r"not really")
ABANDON = _pattern(r"cancel", r"never ?mind", r"forget it", r"stop", r"don'?t want")
SMALL_TALK = _pattern(r"hello", r"hi", r"hey", r"help", r"information", r"question")

SLOT_STAGES = ('collect_date', 'collect_time', 'collect_name', 'collect_reason')
STAGE_FIELD = {'collect_date': 'preferred_date', 'collect_time': 'preferred_time',
               'collect_name': 'patient_name', 'collect_reason': 'reason'}
STAGE_PROMPT = {'collect_date': 'ask_preferred_date', 'collect_time': 'ask_preferred_time',
                'collect_name': 'ask_patient_name', 'collect_reason': 'ask_reason_for_visit',
                'confirm': 'confirm_appointment'}
FAQ = (('clinic_hours', HOURS), ('clinic_address', ADDRESS), ('clinic_phone', PHONE),
       ('clinic_email', EMAIL), ('services', SERVICES))


@dataclass
class PhoneTurn:
    intent: str
    prompts: list[str]
    hangup: bool = False
    appointment: dict | None = None
    callback_topic: str | None = None
    state: dict = field(default_factory=dict)


def new_session(direction: str = 'inbound') -> dict:
    return {'stage': 'idle', 'silence': 0, 'unclear': 0, 'turns': 0, 'direction': direction,
            'appointment': {}, 'last_prompt': 'outbound_greeting' if direction == 'outbound' else 'greeting'}


def greeting_prompt(direction: str) -> str:
    return 'outbound_greeting' if direction == 'outbound' else 'greeting'


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:MAX_FIELD_CHARS]


def respond(state: dict, speech: str, *, max_turns: int = 24) -> PhoneTurn:
    state = {**new_session(state.get('direction', 'inbound')), **state}
    state['appointment'] = dict(state.get('appointment') or {})
    state['turns'] += 1
    text = _clean(speech)
    turn = _respond(state, text, max_turns)
    state['last_prompt'] = turn.prompts[-1] if turn.prompts else state.get('last_prompt')
    turn.state = state
    return turn


def _goodbye(state: dict, intent: str = 'goodbye') -> PhoneTurn:
    state['stage'] = 'ended'
    return PhoneTurn(intent, ['goodbye'], hangup=True)


def _faq(state: dict, intent: str, asset: str) -> PhoneTurn:
    state['stage'] = 'idle'
    state['unclear'] = 0
    return PhoneTurn(intent, [asset, 'anything_else'])


def _respond(state: dict, text: str, max_turns: int) -> PhoneTurn:
    if state['turns'] > max_turns:
        return _goodbye(state, 'turn_limit')
    stage = state['stage']
    if not text:
        state['silence'] += 1
        if state['silence'] > MAX_SILENT_TURNS:
            return _goodbye(state, 'silence_timeout')
        if stage in STAGE_PROMPT:
            return PhoneTurn('silence_reprompt', ['repeat_or_not_understood', STAGE_PROMPT[stage]])
        return PhoneTurn('silence', ['repeat_or_not_understood'])
    state['silence'] = 0

    if EMERGENCY.search(text):
        state['appointment'] = {}
        return _faq(state, 'emergency', 'emergency_care')

    if stage in SLOT_STAGES:
        if ABANDON.search(text):
            state['appointment'] = {}
            return _faq(state, 'appointment_abandoned', 'appointment_declined')
        if END.search(text):
            return _goodbye(state)
        state['appointment'][STAGE_FIELD[stage]] = text
        next_stage = 'confirm' if stage == SLOT_STAGES[-1] else SLOT_STAGES[SLOT_STAGES.index(stage) + 1]
        state['stage'] = next_stage
        return PhoneTurn(f'appointment_{STAGE_FIELD[stage]}', [STAGE_PROMPT[next_stage]])

    if stage == 'confirm':
        if AFFIRM.search(text) and not NEGATE.search(text):
            details = state['appointment']
            state['appointment'] = {}
            state['stage'] = 'idle'
            return PhoneTurn('appointment_confirmed', ['appointment_recorded', 'anything_else'], appointment=details)
        if NEGATE.search(text) or ABANDON.search(text):
            state['appointment'] = {}
            return _faq(state, 'appointment_declined', 'appointment_declined')
        state['unclear'] += 1
        if state['unclear'] >= 2:
            state['appointment'] = {}
            return _faq(state, 'appointment_unconfirmed', 'appointment_declined')
        return PhoneTurn('confirm_unclear', ['repeat_or_not_understood', 'confirm_appointment'])

    # Idle: answer questions and route requests.
    if UNSUPPORTED.search(text):
        state['unclear'] = 0
        return PhoneTurn('unsupported_question', ['unsupported_question', 'anything_else'], callback_topic=text)
    if APPOINTMENT.search(text):
        state['unclear'] = 0
        state['stage'] = 'collect_date'
        state['appointment'] = {}
        return PhoneTurn('appointment_request', ['appointment_request'])
    if HUMAN.search(text):
        state['unclear'] = 0
        return PhoneTurn('human_callback', ['callback_noted', 'anything_else'], callback_topic=text)
    for intent, pattern in FAQ:
        if pattern.search(text):
            return _faq(state, intent, intent)
    if END.search(text):
        return _goodbye(state)
    after_offer = state.get('last_prompt') == 'anything_else'
    if after_offer and NEGATE.search(text):
        return _goodbye(state)
    if THANKS.search(text):
        return _goodbye(state) if after_offer else PhoneTurn('thanks', ['anything_else'])
    if after_offer and AFFIRM.search(text) or SMALL_TALK.search(text):
        state['unclear'] = 0
        return PhoneTurn('offer_help', ['generic_help'])
    state['unclear'] += 1
    if state['unclear'] >= MAX_UNCLEAR_TURNS:
        state['unclear'] = 0
        return PhoneTurn('unclear_callback', ['unsupported_question', 'anything_else'], callback_topic=text)
    return PhoneTurn('unclear', ['generic_help'])
