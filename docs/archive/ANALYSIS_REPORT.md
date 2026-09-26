> **Historical report (superseded 2026-09-26).** Its voice-pack, preflight and call claims are not current: the previous voice pack was synthetic sine tones and has been replaced by an ASR-verified Fish reference-voice pack. See README.md and `GET /api/twilio/preflight` for the current state.

# Mouth Care Solutions Project Analysis

## Executive assessment

The archive contains a coherent **demo/pilot full-stack application** for an AI dental-clinic receptionist. It includes a FastAPI backend, React/Vite frontend, in-memory booking logic, a PostgreSQL schema outline, Twilio/LLM/provider seams, approved clinic knowledge, Docker files, and automated tests. The implementation is suitable for a controlled demo, but it is **not safe to deploy as-is for real patient traffic or real telephony**.

The most urgent issue is that the archive includes a `.env` file containing live-mode configuration and provider credentials. Those credentials must be treated as compromised and rotated or revoked immediately. The project’s own `.gitignore` excludes `.env`, but the submitted archive still contains it.

## What the system does

The backend exposes clinic-information and safe-emergency responses, appointment slot discovery, hold/confirm/reschedule/cancel flows, callback requests, call-request flows, Twilio TwiML endpoints, usage metrics, and a local admin demonstration. The React client provides a polished receptionist workspace with chat, browser speech recognition/synthesis hooks, appointment display, callback form, clinic information, and demo/live status labeling.

The design deliberately limits clinical answers to approved facts and refuses to invent prices, doctors, insurance, or other unspecified details. That safety boundary is a strong aspect of the project.

## Validation performed

| Check | Result | Notes |
|---|---:|---|
| Python compilation | Pass | `python3 -m compileall -q backend` |
| Backend tests with bundled `.env` | **72 passed, 7 failed** | The bundled environment switches the app to live mode, breaking demo-oriented tests |
| Backend tests with `.env` temporarily removed | **79 passed** | Confirms the included suite is mostly green in the intended default demo configuration |
| Zero-error scan with `.env` temporarily removed | Pass | `ZERO_ERROR_SCAN_PASS` |
| Frontend tests | **2 passed** | Vitest |
| Frontend production build | Pass | Vite build completed successfully |
| Docker/provider end-to-end operation | Not verified | Requires external services and credentials |

The test suite is therefore not reproducible from the supplied archive without manually removing or renaming `.env`.

## Findings by severity

### Critical: credentials are included in the archive

`/.env` is present in the ZIP and contains live mode settings, a Twilio account identifier, a Twilio authentication token, phone configuration, and a public webhook URL. The file is not merely an example file. This conflicts with the README statement that secrets should never be committed and with the packaging claim that the deliverable excludes secrets.

**Impact:** An attacker who obtains the archive may place calls, receive or forge provider callbacks, incur charges, or access other provider capabilities depending on the credential scope.

**Required action:** Revoke/rotate the exposed Twilio credential immediately; rotate any other values that were used outside the demo; remove `.env` from the archive and any distribution history; provide only `.env.example` with placeholders; audit provider logs for unauthorized activity.

### High: Twilio signature validation is incomplete

The inbound webhook route validates signatures in live mode, but the status callback route, speech follow-up route, and outbound TwiML route do not call the shared `_validate_twilio_request` helper. These endpoints can therefore accept unauthenticated requests that alter call status, invoke agent processing, or retrieve TwiML behavior.

**Required action:** Validate `X-Twilio-Signature` against the exact externally visible URL and parsed form parameters on every Twilio POST endpoint. Add replay protection where appropriate, reject unexpected parameters, and add negative tests for each route.

### High: demo/live configuration is unsafe and non-reproducible

Because `.env` is loaded automatically and sets `APP_MODE=live` / `MOCK_MODE=false`, a fresh test run from the archive starts in live mode. This causes readiness to return 503 and changes admin, telephony, and call-request behavior. Seven included tests fail in that state, while 79 pass only after temporarily removing `.env`.

**Required action:** Never distribute a populated `.env`; make demo mode the explicit default; add a CI test that starts from a clean environment; fail fast on unsafe live configuration rather than silently starting with partially configured integrations.

### High: live admin authentication is weak

The live login path compares the submitted password directly with `ADMIN_INITIAL_PASSWORD`, while the demo path uses PBKDF2 hashing. The custom signed token has no expiry, audience, issuer, refresh/revocation mechanism, or login rate limiting. The admin authorization check also accepts any signed payload ending in `:admin`.

**Required action:** Store password hashes only; use a standard session/JWT library with short-lived tokens and claims; implement RBAC, revocation, rate limiting, audit-safe logging, and CSRF protection for browser sessions.

### High: patient data is retained in process memory and event logs

The repository is in-memory, callback requests retain names and contact details, booking objects retain patient details, and event logging can record session identifiers and operational data. Restarting the process loses bookings and callbacks, while a production process can accumulate PII without retention controls.

**Required action:** Use the PostgreSQL repository before live operation; minimize and encrypt sensitive fields where appropriate; redact logs; define retention/deletion policies; restrict access to audit data; add durable queues for callbacks and notifications.

### Medium: the direct-call UX and backend target are misleading

The frontend’s “Request a call” shortcut sends the clinic’s publicly displayed phone number as `phone_number`. The backend verifies that number equals the clinic number and then calls the clinic number. The UI copy says “Calling you…” / “Your phone should ring shortly,” which does not match the actual destination behavior.

**Required action:** Either make this explicitly a clinic-dial demo or collect and validate the patient’s phone number, display the destination clearly, and add consent and abuse controls before placing calls.

### Medium: appointment operations lack production concurrency controls

The demo repository performs hold, confirm, reschedule, and cancel operations in process memory without database transactions or row-level locking. The idempotency map is also process-local. Concurrent workers could double-book slots or lose idempotency state.

**Required action:** Implement transactional PostgreSQL operations, unique constraints for slot occupancy, durable idempotency keys, hold expiry jobs, and integration tests under concurrent requests.

### Medium: API abuse controls are absent

Public agent, callback, call-request, booking, and telephony routes have no visible rate limiting, request-size limits, phone validation/normalization, CAPTCHA/abuse mitigation, or per-session quotas. The callback endpoint accepts contact data and returns queued status without a durable notification workflow.

**Required action:** Add rate limiting, schema constraints, normalization, abuse monitoring, request correlation IDs, and a durable notification provider with failure handling.

### Low/medium: documentation and implementation have drifted

The audit documentation claims 78 backend tests passed and describes production verification, while the current archive yields 79 tests in clean demo mode and 72/79 with the bundled `.env`. The docs also claim the packaged deliverable excludes secrets, which is false for this archive.

**Required action:** Generate validation evidence from the exact release artifact, update test counts automatically, and add a release check that rejects `.env`, caches, build artifacts, and secret-like files.

## Positive aspects

- The approved-knowledge boundary is clear and conservative.
- Emergency responses explicitly avoid diagnosis and direct users to urgent care when appropriate.
- Consent is required for booking confirmation and callback requests.
- Booking confirmation includes idempotency handling in demo mode.
- Frontend build and tests are clean.
- The project includes health/readiness routes, a migration outline, documentation, provider seams, and a useful production-gap register.
- TwiML output escapes generated speech text, reducing XML injection risk in that path.

## Recommended release decision

**Approve for internal demo only, with the supplied `.env` removed and credentials rotated. Do not enable live telephony, patient scheduling, or production admin access until the critical/high findings are remediated and independently tested.**

## Suggested remediation order

1. Revoke/rotate all exposed provider credentials and remove `.env` from the release artifact and history.
2. Fix Twilio signature validation on every provider callback and add negative tests.
3. Make clean demo mode the only default and add a release/package secret scan.
4. Replace live admin password comparison and custom indefinite tokens with secure session/RBAC handling.
5. Implement durable PostgreSQL booking/callback persistence with transactional concurrency controls.
6. Add rate limiting, PII minimization/redaction, retention, monitoring, and provider failure handling.
7. Correct the direct-call UX and re-run an end-to-end test against isolated provider test credentials.

## Files reviewed

`README.md`, `BUILD_REPORT.md`, `docs/code-audit.md`, `docs/security.md`, `docs/production-gap-register.md`, backend application/configuration/services/adapters/tests, frontend source/configuration, `.env`, `.env.example`, Docker configuration, and the initial SQL migration.

*Credential values are intentionally not reproduced in this report.*
