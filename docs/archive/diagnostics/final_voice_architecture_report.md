> **Historical report (superseded 2026-09-26).** Its voice-pack, preflight and call claims are not current: the previous voice pack was synthetic sine tones and has been replaced by an ASR-verified Fish reference-voice pack. See README.md and `GET /api/twilio/preflight` for the current state.

# Final Voice Architecture Report

## Status

**PARTIAL/BLOCKED**

The application and public runtime gates pass, but the single controlled Fish Speech reference-conditioned generation did not return an HTTP response or WAV. No Fish-conditioned asset pack or real Twilio call is claimed.

## Project

- Call provider: `twilio`
- Mock mode: `false`
- From: `+17372508034`
- Authorized destination: `+919908552414`
- Clinic phone was not used as the destination.

## Fish Speech

- `/v1/health`: HTTP 200, `{"status":"ok"}`
- One listener owns port 8080.
- Reference: `fish-references/mouth-care-receptionist-reference-20260919.wav`
- Verified format: 44.1 kHz, mono, signed 16-bit PCM WAV.
- The official `api_client.py` request used `ServeReferenceAudio`/`ServeTTSRequest`, `?format=msgpack`, the exact reference WAV and approved transcript, and `max_new_tokens=32`. The Fish server was restarted with CPU `torch.float32`; the request still produced no HTTP response or output WAV. The stale client was stopped; no Fish-conditioned output is claimed.

## Voice Pack

- 16 required assets exist under `artifacts/voice-pack`.
- Manifest, SHA256 hashes, WAV parsing, and telephone format validation pass.
- Every production asset is 8 kHz, mono, 16-bit PCM with nonzero frames.
- Assets are marked `preexisting_asset_unverified`; they are not falsely labeled as successful Fish HTTP generations.
- Missing dedicated responses resolve to `generic_help.wav`.

## Runtime

- `/ready`: HTTP 200.
- `VOICE_PACK_READY` is independent from `FISH_SPEECH_READY`.
- Production Twilio turns use the immutable voice pack and `<Play>`.
- No active `synthesize(` call remains in `backend/app/main.py`.
- Runtime logs include `source=voice_pack fish_runtime_inference=false`.

## Public Endpoint

- Ngrok target: `http://127.0.0.1:8000`.
- Public `/ready`: HTTP 200.
- `/api/twilio/preflight`: not accepted as green because the reference-conditioned Fish generation gate is false.
- Public greeting and fallback audio routes: HTTP 200, `audio/wav`.
- Valid Twilio signature: HTTP 200 XML containing `<Play>`.
- Invalid Twilio signature: HTTP 403.
- Active outbound validation now uses `twilio.request_validator.RequestValidator` with the exact configured public URL. Focused regression: valid signature HTTP 200, invalid signature HTTP 403.

## Tests

- Backend pytest: `103 passed, 1 warning`.
- Backend `compileall`: passed.
- Frontend `npm run build`: passed.
- Voice-pack validator: passed.

## Real Call

- Historical call SID: `CA73d01c01a354ae407be1074c278b89b9`
- New direct call SID: `CA9549251823bf169dcff69951e8851733`.
- Twilio status: `completed`.
- Duration: 5 seconds.
- Error code/message: none returned by Twilio.
- From/to: `+17372508034` -> `+919908552414`.
- Request path: `POST /api/calls/request`.
- Selected greeting: `greeting.wav` at `https://data-colonist-tinker.ngrok-free.dev/demo/api/telephony/audio/greeting`.
- `reference_voice_asset_verified=false` because the manifest marks the asset `preexisting_asset_unverified`.
- `fish_runtime_inference=false`.
- The earlier call's outbound webhook returned HTTP 403, so no greeting audio request or follow-up webhook was observed. After the fix, a synthetic public outbound request returned HTTP 200 with `<Play>` for `greeting.wav` and no `<Say>`. No second real call was made.
- Latest direct call: `CAf88c797047a6d135a84ba8932bf00df8`, Twilio `completed`, duration 5 seconds, no Twilio error. Its outbound webhook returned HTTP 403 because the captured request had no `X-Twilio-Signature` header. Status callback returned HTTP 200; no greeting audio request was observed. No retry was made.

## Reproducible Generation

The sequential HTTP generator is [scripts/generate_voice_pack_http.py](../scripts/generate_voice_pack_http.py). It sends one reference-conditioned msgpack request at a time, converts each response with ffmpeg to telephone WAV, validates it, and writes hashes to the manifest.

## Known Limitations

- Remaining blockers: Fish reference-conditioned generation remains unverified, and the live Twilio request was unsigned, so the active validator correctly returned HTTP 403. The latest call was not retried.

## Inline TwiML Attempt

- The inline TwiML contained `<Play>` for the public `greeting.wav` URL, a `/voice` Gather action, no `<Say>`, and no `/outbound` URL.
- The single call-creation attempt returned HTTP 502 before Twilio returned a Call SID.
- Twilio's recent call list contains no new call from this attempt, and no new ngrok outbound or greeting-audio request was observed.
- No retry was made.

## Latest Debug Attempt

- The official Twilio SDK path (`twilio 9.11.1`, `Client.calls.create`) was exercised once through the real endpoint.
- Creation returned HTTP 502 with no Call SID, outbound webhook, or greeting-audio request.
- The backend now logs redacted `TwilioRestException` type, HTTP status, provider code, message, and `more_info`; credentials are excluded.
- No retry was made.

## Current Preflight

- Twilio authentication: passed.
- Configured From: `+17372508034`.
- Twilio account phone inventory: zero owned IncomingPhoneNumbers and zero verified outgoing caller IDs; `from_number_authorized=false`.
- Packaged greeting/public audio, signature validation, and inline TwiML checks pass.
- `live_call_allowed=false` with blocker `from_number_not_authorized_for_account`.
- No new call was created during this repair pass.
- Current loaded account type: `Trial`; destination `+919908552414` is not present in verified outgoing caller IDs, so `destination_authorized=false`.
- No release ZIP exists in the project root, so no `.env` archive was found or modified.
