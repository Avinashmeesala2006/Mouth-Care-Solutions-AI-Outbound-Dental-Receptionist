"""Transcript rules shared by the voice-pack generator and the runtime validator."""
import json
from pathlib import Path

import pytest

from backend.app.services.voice_pack import load_prompts, transcript_problems

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = load_prompts(ROOT / 'knowledge' / 'clinic' / 'voice_prompts.json')
APPROVED = json.loads((ROOT / 'knowledge' / 'clinic' / 'approved.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('asset_id,heard,problem', [
    ('emergency_care', 'If there is severe pain, uncontrolled bleeding, facial swelling affecting bleeding or swallowing, '
                       'or a dental injury, seek urgent medical care. This receptionist cannot diagnose.',
     'asr_required_phrase_missing:breathing or swallowing'),
    ('clinic_email', 'our email address is mouth care solutions all one word at email dot com', 'asr_required_phrase_missing:gmail'),
    ('generic_help', 'I can help with clinic hours, our RRS, our RRS, our services, emergencies, or an appointment request.',
     'asr_repeated_phrase:our rrs'),
    ('ask_reason_for_visit', 'Could you please please tell me the reason for the visit?', 'asr_repeated_word:please'),
])
def test_defective_takes_observed_in_the_previous_pack_are_rejected(asset_id, heard, problem):
    assert problem in transcript_problems(heard, PROMPTS[asset_id])


@pytest.mark.parametrize('asset_id', sorted(PROMPTS))
def test_every_approved_prompt_passes_its_own_rules(asset_id):
    assert transcript_problems(PROMPTS[asset_id]['text'], PROMPTS[asset_id]) == []


def test_required_phrase_alternatives_and_legit_repeats():
    assert transcript_problems('write to mouth care solutions all one word at g mail dot com', PROMPTS['clinic_email']) == []
    phone = PROMPTS['clinic_phone']
    assert not [p for p in transcript_problems('plus nine one nine six four two two three', phone) if 'repeated' in p]


def test_emergency_prompt_uses_approved_emergency_wording():
    approved_emergency = ('If there is severe pain, uncontrolled bleeding, facial swelling affecting breathing or swallowing, '
                          'or a dental injury, seek urgent medical care. This receptionist cannot diagnose.')
    assert PROMPTS['emergency_care']['text'] == approved_emergency


def test_spoken_clinic_facts_match_approved_knowledge():
    facts = APPROVED['facts']
    assert 'mouthcaresolutions@gmail.com' == facts['email']
    assert 'gmail' in PROMPTS['clinic_email']['text'] and 'mouth care solutions' in PROMPTS['clinic_email']['text']
    assert facts['phone'] == '+91 96423 40630'
    assert 'nine six four two three, four zero six three zero' in PROMPTS['clinic_phone']['text']
    assert 'Monday to Saturday' in PROMPTS['clinic_hours']['text'] and 'closed on Sundays' in PROMPTS['clinic_hours']['text']
    for place in ('Bhavani Complex', 'Suryaraopeta', 'Vijayawada', 'Andhra Pradesh'):
        assert place in PROMPTS['clinic_address']['text'] and place in facts['address']
    for category in facts['service_categories']:
        spoken = PROMPTS['services']['text'].lower()
        assert category.lower() in spoken or category.lower() == 'emergency care' and 'emergency dental care' in spoken
