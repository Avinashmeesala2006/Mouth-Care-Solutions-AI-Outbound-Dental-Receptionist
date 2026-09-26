# Code Audit

| File/area | Issue | Severity | Root cause | Fix | Proof |
|---|---|---:|---|---|---|
| `knowledge/clinic/approved.json`, source, docs | Previous clinic phone appeared in the prior project version | High | Phase-one configuration was hard-coded | Repository-wide replacement and centralized `CLINIC_PHONE` | Repository-wide retired-number scan returns no matches |
| `backend/app/adapters/llm.py` | Live mode could fall back to mock when no API key was present | Critical | Selector treated missing credentials as demo behavior | Live mode now raises an explicit configuration error | `test_live_llm_does_not_fallback` |
| `backend/app/core/config.py` | Required phase-three settings were incomplete | High | Initial settings were minimal demo settings | Added clinic email, database, provider, admin, frontend, and live integration fields plus validation | compile/test and `/ready` checks |
| `backend/app/main.py` | Legacy mock validation endpoint contradicted the required Twilio endpoint | High | Phase-two compatibility route remained undocumented | Removed route; retained the Twilio webhook with HMAC validation seam | endpoint tests and OpenAPI route inspection |
| `backend/app/main.py` | Readiness always reported demo/in-memory | High | Health response was hard-coded | Reports configured mode and refuses live readiness until PostgreSQL booking repository is configured | health/readiness tests |
| `frontend/src/main.jsx` | Browser voice input was absent | Medium | Client only implemented text chat | Added SpeechRecognition input states; no browser speech output is used for phone calls | source inspection; backend regression suite |
| `backend/app/adapters/*` | Provider seams lacked a single audit entry | Medium | Providers were added incrementally | Added LLM, Twilio, PostgreSQL, auth, voice, calendar, and notification seams | import/adapter tests |

## Audit commands

- Python compile check: `python3 -m compileall -q backend`
- Regression suite: `pytest -q`
- Stale phone scan: repository-wide search for the retired number, excluding caches and archives
- Secret/archive scan: ZIP excludes `.env`, `.venv`, caches, logs, and build artifacts.

## Known intentional boundaries

`DEMO MODE` uses mock providers and an in-memory repository by design. `LIVE MODE` does not fall back to demo behavior: readiness fails with explicit missing configuration, including the not-yet-enabled live PostgreSQL booking repository. Production gaps are documented rather than silently represented as complete.

No TODO/FIXME marker represents an unacknowledged required feature. Remaining live dependencies are explicit configuration or integration boundaries in the production-gap register.

## UX and telephony upgrade audit — 2026-09-04

The basic single-answer React client was replaced with a responsive receptionist workspace: conversational message bubbles, timestamps, loading indicators, quick prompts, clear conversation, browser voice states, structured appointment cards, callback form, call-request status, clinic facts, mode labeling, responsive layout, and visible error states. The browser calls backend routes only and contains no Twilio credentials.

The backend now exposes safe `GET /api/status`, `POST /api/calls/request`, `GET /api/calls/{request_id}`, and `POST /api/telephony/twilio/status-callback` routes. Demo call requests remain explicitly simulated. Live call requests are credential-gated and use the Twilio REST endpoint through the backend; provider failures return a safe error without exposing provider details. Twilio inbound and follow-up routes validate the actual request URL and parsed POST parameters in LIVE mode using `X-Twilio-Signature`; DEMO MODE uses the isolated mock path.

Verification evidence: `pytest -q` reported 78 backend tests passed; clean frontend `npm ci`, `npm run test` (2 tests), and `npm run build` passed; the zero-error scanner passed; runtime checks covered root, favicon, health, readiness, demo, status, call request, Twilio webhook, Twilio follow-up, and OpenAPI. Docker and paid-provider end-to-end execution remain not verified because those external capabilities are unavailable in the sandbox.
