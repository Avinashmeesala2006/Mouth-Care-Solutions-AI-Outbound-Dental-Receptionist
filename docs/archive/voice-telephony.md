# Voice and telephony

VoiceProvider and telephony adapters are provider-neutral interfaces. ElevenLabs and Twilio configuration must be supplied separately. In LIVE mode, Twilio webhook validation uses the actual request URL, POST parameters, and `X-Twilio-Signature`; DEMO MODE uses isolated mock behavior and never claims a real call.
