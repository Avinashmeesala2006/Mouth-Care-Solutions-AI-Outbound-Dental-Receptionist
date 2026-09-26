> **Historical report (superseded 2026-09-26).** Its voice-pack, preflight and call claims are not current: the previous voice pack was synthetic sine tones and has been replaced by an ASR-verified Fish reference-voice pack. See README.md and `GET /api/twilio/preflight` for the current state.

# Build Report

## Implemented

A mock-mode end-to-end AI receptionist pilot for Mouth Care Solutions: deterministic policy and approved knowledge lookup, typed booking tools with holds/idempotency/state transitions, callback and human escalation, usage metering, audit events, health/readiness, admin summary/config endpoints, mock voice/telephony/notifications, a browser demo, React/Vite source, tests, Docker configuration, PostgreSQL migration, and operational documentation.

## Stack

Python 3.11, FastAPI, Pydantic, Uvicorn, pytest; React 18 + Vite source; Docker Compose; PostgreSQL migration target; provider-neutral adapters.

## Tested

The test suite covers clinic facts, services, emergency safety, unsupported facts, booking confirmation, idempotency, expired holds, rescheduling, cancellation, callbacks, escalation, usage thresholds, prompt-injection resistance, invalid webhook signatures, and unauthorized admin behavior.

## Run

`pip install -r backend/requirements.txt && uvicorn backend.app.main:app --reload`, then open `/demo`. Or `docker compose up --build`.

## Mock mode

Works without paid providers. Demo slots and mock references are clearly marked `DEMO-`.

## Credentials required

ElevenLabs, Twilio, Google Calendar, real notification provider, and durable production database integrations require configuration and provider accounts. They are not claimed active.

## Limitations / production gaps

The runtime repository is in-memory for portable demo execution; browser authentication is demo-only; vector retrieval, real voice streaming, durable scheduling, consent/retention, and clinical operations remain configuration/integration work. See `docs/production-gap-register.md`.

## Phase 2 verification

The existing project was upgraded in place. The active configured phone is **+91 96423 40630**, sourced from `CLINIC_PHONE=+919642340630`; this is a project configuration override and is not a claim that the number is provisioned in Twilio.

Added provider seams for a mock/live LLM, ElevenLabs voice, Twilio request signature validation and TwiML, optional PostgreSQL health/transaction access, Google Calendar, notifications, password hashing, and live-mode configuration. Added phase-two documentation and phone test procedure. DEMO MODE remains the tested default.

The final backend suite has **79 tests passed** with `pytest -q`; the frontend suite has **2 tests passed** with `npm run test`. The frontend production build passed with `npm run build` after a clean `npm ci`. Root, favicon, health, readiness, polished demo UI, configured clinic phone, safe call-request/status APIs, demo outbound call contract, approved XML-safe TwiML greeting, live outbound Twilio adapter, mock Twilio webhook and follow-up voice route, booking/idempotency, prompt-injection defense, password hashing, and Twilio signature validation were smoke-tested. Python compilation passed, and the repository-wide stale-number scan passed.

LIVE MODE is not claimed active. It requires real credentials, HTTPS, PostgreSQL, a provisioned telephony number, an approved ElevenLabs agent, live scheduling, consent/recording policy, and operational testing. The runtime repository remains in-memory in DEMO MODE; the PostgreSQL layer is an optional seam and migration target.

## Final ZIP

`Mouth_Care_Solutions_AI_Receptionist_FINAL_REPAIRED_COMPLETE.zip`

## Final QA release gate

The final repaired release archive is `Mouth_Care_Solutions_AI_Receptionist_FINAL_REPAIRED_COMPLETE.zip`. It includes the pinned frontend lockfile, code audit, authentication/session seam, fail-closed live configuration, root/favicon handling, Docker frontend service, exact `/api/call-request` contract, Twilio outbound adapter, and final QA coverage. The clinic destination is `+919642340630`; the configured trial caller is `+17372212163`. Docker commands and a physical Twilio call were not executable in the build sandbox because Docker and live Twilio credentials/public HTTPS were unavailable; both must be verified in the deployment environment before claiming live calling.


## Phase 2 upgrade note

The configured clinic phone is **+91 96423 40630** via `CLINIC_PHONE=+919642340630`; this is a project configuration override, not a claim from the original source. Added LLM, Twilio signature-validation, ElevenLabs, optional PostgreSQL, password hashing, and live-mode seams. DEMO MODE remains the tested default. LIVE MODE is not claimed active without credentials, provisioning, HTTPS, database, and operational approvals.
