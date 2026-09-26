# API

FastAPI OpenAPI is available at `/docs`. Endpoints: agent session, knowledge search, callback,
demo booking (slots, hold, confirm, reschedule, cancel; HTTP 503 in live mode because no
production booking repository is connected), usage, health (`/health`), application readiness
(`/ready`), `GET /api/status` (cheap status with the cached live-call readiness),
`GET /api/telephony/preflight` (full live-call readiness, see docs/telephony-asterisk.md),
`GET /api/telephony/audio/{asset_id}` (verified Fish voice assets), and protected admin
config/activity.

Calls: `POST /api/calls/request` is the only outbound entry point. It validates consent and an
E.164 destination, applies the `OUTBOUND_ALLOWED_DESTINATIONS` allow-list and a per-destination
rate limit, runs a fresh preflight and originates through Asterisk only when
`LIVE_CALL_ALLOWED` is true. `GET /api/calls/{request_id}` returns the event-driven status;
`GET /api/calls/{request_id}/events` and `POST /api/calls/{request_id}/hangup` require an admin
token. Demo mode (`MOCK_MODE=true`) records requests and never places a call.
