# Deployment

Local Windows host: `scripts/run_local_stack.ps1` starts or reuses Fish Speech (:8080), FastAPI
(:8000) and, if `PUBLIC_BASE_URL` is set, the ngrok tunnel for the web application, then prints
the live-call preflight. Asterisk runs in WSL2 (`scripts/setup_asterisk.ps1`,
`scripts/asterisk_ctl.ps1`); see docs/telephony-asterisk.md. The public URL serves the web
application only; it does not provide telephone connectivity.

Docker Compose runs the API and the Nginx frontend (port 8080, proxying `/api/`). Asterisk is not
part of the compose file; point `ASTERISK_AMI_HOST` at a private Asterisk host.

`MOCK_MODE=true` is the portable demo mode. With `MOCK_MODE=false` nothing falls back to mocks:
`/ready` and `/api/telephony/preflight` report every missing piece (Asterisk, phone line,
voice pack, Fish Speech, speech recognition, configuration). For production add managed
PostgreSQL for bookings, HTTPS, a secret manager, monitoring, backups and retention controls.
