# Testing

From the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
Push-Location frontend
npm test
npm run build
Pop-Location
.\.venv\Scripts\python.exe scripts\validate_voice_pack.py
.\.venv\Scripts\python.exe scripts\verify_public_route.py --destination +919908552414
```

The public verifier performs read-only account inspection and route probes; it never creates a call. A nonzero result is expected when Trial or production account requirements are not met. Keep `TRIAL_CALL_ALLOWED` and `LIVE_CALL_ALLOWED` separate when reporting readiness.
