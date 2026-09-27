# Acceptance Checklist

- [x] Twilio is the only active telephony provider.
- [x] Fish Speech packaged voice assets are validated from the canonical reference.
- [x] Twilio webhook signatures are required and tested.
- [x] Outbound calls use one application entry point and idempotent call state.
- [x] Live-call permission is computed from runtime readiness and provider authentication.
- [ ] Configure valid local Twilio credentials, an authorized caller number, PostgreSQL, and a reachable public HTTPS origin.
- [ ] Run public preflight and place exactly one call only when `LIVE_CALL_ALLOWED=true`.
