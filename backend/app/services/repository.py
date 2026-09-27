from datetime import timedelta

from backend.app.models.domain import Booking, BookingStatus, Hold, Slot, now


class Repo:
 def __init__(self):
  base=now().replace(minute=0,second=0,microsecond=0); self.slots=[Slot(f'demo-slot-{i}',base+timedelta(days=i+1,hours=2+i), service_id='general',duration_minutes=30) for i in range(3)]; self.holds={}; self.bookings={}; self.idempotency={}; self.events=[]; self.callbacks=[]; self.usage={'minutes':0,'sessions':0,'estimated_cost':0.0}
 def log(self, kind, data): self.events.append({'kind':kind,'data':data,'at':now().isoformat()})
 def available(self):
  self.expire(); return [s for s in self.slots if not s.booked and not s.held_by]
 def slot_view(self,s): return {'id':s.id,'starts_at':s.starts_at.isoformat(),'duration_minutes':s.duration_minutes,'demo':True}
 def expire(self):
  for token,h in list(self.holds.items()):
   if h.expires_at <= now():
    slot=next(s for s in self.slots if s.id==h.slot_id); slot.held_by=None; slot.hold_until=None; del self.holds[token]; self.log('hold_expired',{'token':token})
 def hold(self, slot_id, session_id, seconds):
  self.expire(); slot=next((s for s in self.slots if s.id==slot_id),None)
  if not slot or slot.booked or slot.held_by: raise ValueError('slot_unavailable')
  token='DEMO-HOLD-'+__import__('secrets').token_hex(4).upper(); exp=now()+timedelta(seconds=min(seconds,600)); slot.held_by=session_id; slot.hold_until=exp; self.holds[token]=Hold(token,slot_id,session_id,exp); self.log('slot_held',{'slot_id':slot_id,'session_id':session_id}); return {'hold_token':token,'expires_at':exp.isoformat()}
 def confirm(self, token, patient, consent, key):
  self.expire()
  if not consent: raise ValueError('explicit_consent_required')
  if key in self.idempotency: return self.idempotency[key]
  h=self.holds.get(token)
  if not h: raise ValueError('hold_expired_or_invalid')
  if not patient.get('name') or not patient.get('phone'): raise ValueError('name_and_phone_required')
  slot=next(s for s in self.slots if s.id==h.slot_id); slot.booked=True; slot.held_by=None; ref='DEMO-'+__import__('secrets').token_hex(4).upper(); b={'booking_reference':ref,'status':BookingStatus.CONFIRMED.value,'slot':self.slot_view(slot),'patient_name':patient['name']}; self.bookings[ref]=Booking(ref,slot.id,patient,BookingStatus.CONFIRMED,key); self.idempotency[key]=b; del self.holds[token]; self.log('booking_confirmed',{'reference':ref}); return b
 def reschedule(self, ref, new_id, verification):
  b=self.bookings.get(ref)
  if not b or b.patient.get('phone') != verification: raise ValueError('booking_not_found_or_verification_failed')
  new=next((s for s in self.slots if s.id==new_id),None)
  if not new or new.booked or new.held_by: raise ValueError('new_slot_unavailable')
  old=next(s for s in self.slots if s.id==b.slot_id); old.booked=False; new.booked=True; b.slot_id=new.id; self.log('booking_rescheduled',{'reference':ref}); return {'booking_reference':ref,'status':'CONFIRMED','slot':self.slot_view(new)}
 def cancel(self, ref, reason, verification):
  b=self.bookings.get(ref)
  if not b or b.patient.get('phone') != verification: raise ValueError('booking_not_found_or_verification_failed')
  s=next(s for s in self.slots if s.id==b.slot_id); s.booked=False; b.status=BookingStatus.CANCELLED; self.log('booking_cancelled',{'reference':ref,'reason':reason}); return {'booking_reference':ref,'status':'CANCELLED'}
