# Mouth Care Solutions AI Outbound Dental Receptionist

AI-powered outbound dental receptionist using Twilio Voice, Fish Speech, FastAPI, and PostgreSQL.

FastAPI, Twilio Voice (speech recognised by `<Gather>`), PostgreSQL, and a reference-conditioned Fish Speech voice pack for a dental receptionist.

## Runtime

The application has one outbound path: `POST /api/calls/request` reserves compliant call state, verifies live readiness, and creates one Twilio call. Twilio requests the signed outbound webhook at `/api/telephony/twilio/outbound`; speech Gather callbacks return Fish Speech WAV assets from `/api/telephony/audio/{asset_id}`. Status callbacks update PostgreSQL call state.

Fish Speech is not synthesized per call on CPU. The validated packaged assets under `artifacts/voice-pack` are generated from `fish-references/mouth-care-receptionist-reference-20260919.wav` and served only after manifest, checksum, format, and reference validation.

## Configuration

Copy `.env.example` to `.env` and set Twilio credentials locally:

- `TWILIO_ENABLED=true`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`
- `PUBLIC_BASE_URL`
- PostgreSQL `DATABASE_URL`

Never commit `.env` or provider credentials. Twilio webhook signatures remain enabled.

## Windows startup

```powershell
.\scripts\run_local_stack.ps1 -Ngrok
```

The script reuses healthy PostgreSQL, Fish Speech, FastAPI, and ngrok processes, applies migrations, checks health, and never places a call. Use `scripts\verify_public_route.py --verify-remote` to run the read-only public and Twilio preflight.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m ruff check backend scripts
.\.venv\Scripts\python.exe scripts\zero_error_scan.py
Push-Location frontend; npm test -- --run; npm run build; Pop-Location
```

A real call to the configured test destination is allowed only when `/api/twilio/preflight?verify_remote=1` reports `LIVE_CALL_ALLOWED=true`. The application never bypasses Twilio account, caller-ID, destination, consent, or public-webhook restrictions.

More detail: [docs/api.md](docs/api.md), [docs/architecture.md](docs/architecture.md), [docs/deployment.md](docs/deployment.md), [docs/security.md](docs/security.md), and [docs/testing.md](docs/testing.md).
