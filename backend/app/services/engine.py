import re
from .repository import Repo
from backend.app.core.config import APPROVED_FACTS

CLINIC = {'name':APPROVED_FACTS['name'],'address':APPROVED_FACTS['address'],'phone':APPROVED_FACTS['phone'],'email':APPROVED_FACTS['email'],'hours':APPROVED_FACTS['hours']}
SERVICES = list(APPROVED_FACTS['service_categories'])
EMERGENCY = 'If there is severe pain, uncontrolled bleeding, facial swelling affecting breathing or swallowing, or a dental injury, seek urgent medical care. This receptionist cannot diagnose. If configured, contact the clinic or emergency services now.'

def _has(text, *words):
    return any(re.search(r'\b' + re.escape(w) + r'\b', text) for w in words)

class Policy:
    def answer(self, text):
        t=text.lower()
        if any(x in t for x in ['ignore previous','system prompt','reveal tool','internal tool','jailbreak']): return 'I can help with Mouth Care Solutions information and appointments, but I cannot reveal internal instructions.'
        if _has(t,'emergency','urgent','bleeding','swelling','knocked out','severe pain'): return EMERGENCY
        if _has(t,'hour','hours','open','opening','timings'): return f"Mouth Care Solutions is open {CLINIC['hours']}."
        if _has(t,'address','located','location'): return f"The clinic is at {CLINIC['address']}."
        if _has(t,'phone','telephone','contact number') or re.search(r'\bcall (?:you|the clinic)\b', t): return f"The clinic phone number is {CLINIC['phone']}."
        if _has(t,'email','e-mail'): return f"The clinic email is {CLINIC['email']}."
        if _has(t,'service','services','treatment','treatments'): return 'Approved service categories include: ' + ', '.join(SERVICES) + '.'
        if _has(t,'price','cost','fee','fees','doctor','insurance') or ('dentist' in t and not _has(t,'appointment','book','schedule')): return 'I do not have an approved answer for that detail. I can create a callback request for the clinic team.'
        return None
class Agent:
    def __init__(self, repo): self.repo=repo; self.policy=Policy()
    def respond(self, message, session_id):
        answer=self.policy.answer(message)
        if answer: self.repo.log('agent_response', {'session_id':session_id}); return {'text':answer,'intent':'info','session_id':session_id}
        if _has(message.lower(),'appointment','book','schedule'):
            return {'text':'I can help with a demo appointment. Please choose a slot below, then I will ask for your name and phone number before confirmation.','intent':'appointment','session_id':session_id,'slots':[self.repo.slot_view(s) for s in self.repo.available()]}
        if _has(message.lower(),'human','person','callback','call back','call me'): return {'text':'I can arrange a callback from the clinic team. Please provide your contact number and preferred time window.','intent':'human_handoff','session_id':session_id}
        return {'text':'I can help with clinic hours, address, services, emergencies, demo appointments, or a callback. What would you like to know?','intent':'unknown','session_id':session_id}
