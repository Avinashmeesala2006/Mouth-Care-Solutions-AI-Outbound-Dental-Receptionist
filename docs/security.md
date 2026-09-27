# Security

Secrets live in `.env` or a secret manager and are never logged or returned. This includes the Twilio auth token, database password, admin password, JWT secret, and model keys. `.env.example` contains placeholders only.

Twilio webhooks fail closed unless `X-Twilio-Signature` matches the exact configured public URL and form parameters. Outbound calls require valid destination, consent, no do-not-call entry, no opt-out, quota, capacity, database, voice-pack, and provider-account readiness.

Phone numbers and provider IDs are masked in logs and admin views. Fish Speech and PostgreSQL remain private; only FastAPI and packaged audio assets are public. Rotate the Twilio auth token immediately if it is exposed.
