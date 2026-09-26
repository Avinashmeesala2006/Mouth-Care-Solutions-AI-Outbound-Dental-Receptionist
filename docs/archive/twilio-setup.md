# Twilio setup

The inbound endpoint is `POST /api/telephony/twilio/webhook`; production outbound calls fetch TwiML from `POST /api/telephony/twilio/outbound`. Demo mode never places real calls. Production custom-webhook mode requires an upgraded active account, an owned Voice-capable caller number, destination authorization, India geographic permission where applicable, signed public webhooks, and the verified packaged Fish voice pack. Configure `APP_MODE=live`, `MOCK_MODE=false`, `CALL_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `OUTBOUND_FROM_NUMBER`, `OUTBOUND_ALLOWED_DESTINATIONS`, and matching `PUBLIC_BASE_URL` / `TWILIO_WEBHOOK_BASE_URL` HTTPS origins. `TWILIO_PHONE_NUMBER` is legacy-only and must not conflict with `OUTBOUND_FROM_NUMBER`.

Before any call, run `python scripts/verify_public_route.py --destination +919908552414`. It checks the active tunnel target, exact public FastAPI process, signed and unsigned webhook behavior, Twilio account authentication and authorization, destination permissions, and the complete checksum-verified Fish voice pack. The report separates `SOFTWARE_READY_FOR_LIVE_CALL`, `TRIAL_CALL_ALLOWED`, and `LIVE_CALL_ALLOWED`; a Trial result never grants production permission.

## Trial real-audio test

Twilio's current [Try out Voice guide](https://www.twilio.com/docs/usage/trials/try-out-voice) documents the `voice_play_audio` template and supports `<Play>` in restricted Trial TwiML. Trial recipients must be verified, calls are limited to the account sign-up country, and the trial Voice number can vary by product and recipient. Confirm the exact Voice Trial From number and copy the API request shown by the authenticated Console. The documented template URL is `https://webhooks.twilio.com/v1/Voice/Template/voice_play_audio`.

Do not assume a production From number is the Trial From number. Do not invent a template parameter for the Fish audio URL: the Console-generated request must establish the exact input contract. This project currently reports Trial-specific blockers when recipient verification, country eligibility, Trial From discovery, or the template audio input is unconfirmed. Do not place a Trial call while `TRIAL_CALL_ALLOWED` is false. Trial template calls are separate from the production custom-webhook flow below.

For the Fish audio test, the required public asset is `https://<PUBLIC_BASE_URL>/api/telephony/audio/outbound_greeting`. Verify it is the manifest-verified reference-derived WAV and use only the documented Trial audio-template request. Do not substitute `<Say>`, another TTS provider, or production webhook parameters into the Trial flow.

## Production custom-webhook call

Production calls use the Twilio Calls API with the application's `Url` and `StatusCallback`; they must not use Trial Console templates. Twilio fetches `POST <PUBLIC_BASE_URL>/api/telephony/twilio/outbound`, which returns `<Play>` for the validated `outbound_greeting` pack asset. Status updates arrive at `POST <PUBLIC_BASE_URL>/api/telephony/twilio/status`. The call path fails closed unless `LIVE_CALL_ALLOWED` is true.

Account-side requirements:

- `OUTBOUND_FROM_NUMBER` must be an owned, voice-capable incoming number on the authenticated Twilio account. In Twilio Console, purchase or configure such a number under that account, then set the local environment value to that exact E.164 number. A verified caller ID is not equivalent to an owned incoming number for production.
- Trial restrictions still apply even when a verified recipient and Trial template work: Trial does not imply production custom-webhook permission. Upgrade the account through Twilio Console before production calling. Do not try production parameters as a way around Trial restrictions.
- After upgrading, open Twilio Voice Geographic Permissions and enable calling to India for an Indian destination. Preflight reports the permission separately; a Trial account may deny API inspection of this setting.
- Configure the public HTTPS webhook base URL exactly. Twilio signatures are validated against the exact public URL and form parameters; signature validation cannot be disabled in live mode.

If preflight is blocked, resolve every reported blocker and rerun the same command. Verify the resulting call SID, provider status, status callbacks, webhook/audio events, and that the caller receives the approved packaged Fish reference voice. Never substitute inline TwiML speech or synchronous Fish synthesis for missing packaged assets.
