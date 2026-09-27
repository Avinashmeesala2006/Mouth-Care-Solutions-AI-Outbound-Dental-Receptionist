# Deployment

On Windows, `scripts/run_local_stack.ps1` starts or reuses PostgreSQL, Fish Speech, FastAPI, and optionally ngrok. It checks health and prints readiness; it never places a call.

Set Twilio credentials only through the local environment or a secret manager: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_ENABLED=true`, and a public HTTPS `PUBLIC_BASE_URL`. Configure Twilio webhooks to `/api/telephony/twilio/outbound` and `/api/telephony/twilio/status`.

Production requires PostgreSQL, strong admin/JWT secrets, strict Twilio signature validation, a reachable public origin, and the validated Fish Speech voice pack.
