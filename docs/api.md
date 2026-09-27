# API

OpenAPI is served at `/docs`.

| Area | Endpoints |
| --- | --- |
| Health | `GET /health`, `GET /ready`, `GET /api/voice/fish/health`, `GET /api/telephony/twilio/health` |
| Readiness | `GET /api/twilio/preflight?destination=...&verify_remote=1`, `GET /api/status` |
| Twilio | `POST /api/telephony/twilio/outbound`, `POST /api/telephony/twilio/gather`, `POST /api/telephony/twilio/status` |
| Audio | `GET /api/telephony/audio/{asset_id}` serves verified packaged Fish Speech WAV assets |
| Web receptionist | `POST /api/agent/session`, `POST /api/knowledge/search`, `POST /api/callback` |
| Web call request | `POST /api/calls/request`, `GET /api/calls/{id}` |
| Admin and compliance | `/api/admin/*`, `GET /api/calls/{id}/events`, `POST /api/calls/{id}/hangup` |

Twilio webhook requests require `X-Twilio-Signature` calculated from the configured public URL.
Outbound requests fail closed on invalid destination, consent, do-not-call, opt-out, quota,
capacity, configuration, database, voice-pack, or provider-account checks.
