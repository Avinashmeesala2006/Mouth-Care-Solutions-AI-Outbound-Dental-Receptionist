# Testing

Run backend tests with `\.venv\Scripts\python.exe -m pytest backend/tests -q`. The suite covers Twilio signatures, TwiML webhooks, call reservation, compliance, call state, Fish voice-pack validation, audio conversion, receptionist behavior, and PostgreSQL contracts when `TEST_DATABASE_URL` is set.

Run `\.venv\Scripts\python.exe -m ruff check backend scripts` and `\.venv\Scripts\python.exe scripts\zero_error_scan.py` for lint and release checks. Run frontend checks with `Push-Location frontend; npm test -- --run; npm run build; Pop-Location`.

Automated tests never place calls. A real call is permitted only when `/api/twilio/preflight?destination=...&verify_remote=1` returns `LIVE_CALL_ALLOWED=true`.
