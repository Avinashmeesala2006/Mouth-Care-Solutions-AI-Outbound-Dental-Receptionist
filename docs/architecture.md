# Architecture

Customer calls reach Twilio Voice API, which calls signed FastAPI TwiML webhooks. FastAPI creates calls through the Twilio REST API, serves the approved Fish Speech WAV voice pack over HTTPS, and routes speech Gather results through the grounded receptionist engine.

PostgreSQL stores calls, provider events, conversation state, appointment requests, callbacks, consent, do-not-call, opt-out, quota, and usage data. Development may use the in-memory repository only when PostgreSQL is not configured.

The active telephony modules are `backend/app/telephony/twilio.py` and `backend/app/telephony/twilio_routes.py`. Fish Speech is the only speech source for telephone audio; live CPU synthesis remains disabled for calls.
