> **Historical report (superseded 2026-09-26).** Its voice-pack, preflight and call claims are not current: the previous voice pack was synthetic sine tones and has been replaced by an ASR-verified Fish reference-voice pack. See README.md and `GET /api/twilio/preflight` for the current state.

# Fish Speech Voice Integration — Implementation Report

## Implementation completed

The supplied Mouth Care Solutions project was modified in place. The existing receptionist agent and business logic remain intact. The active live telephony voice path now supports:

> Agent response text → local Fish Speech `/v1/tts` → validated 8 kHz mono 16-bit WAV → short-lived FastAPI audio URL → Twilio `<Play>`

The existing Twilio `<Say>` path remains only for demo compatibility when Fish Speech is disabled. When `FISH_SPEECH_ENABLED=true`, the inbound, outbound, and follow-up voice routes use Fish Speech audio and do not emit `<Say>`.

### Files added

- `backend/app/adapters/fish_speech.py`: provider adapter, audio validation, FFmpeg telephone conversion, TTL audio store, latency/RTF logging.
- `backend/tests/test_fish_speech.py`: adapter, audio-format, TwiML, integration, and live-signature tests.
- `docs/fish-speech.md`: installation, configuration, hardware, API, audio, and operational documentation.
- `scripts/run_fish_speech.sh`: configurable Fish Speech checkout/server launcher.
- `IMPLEMENTATION_REPORT.md`: this report.

### Files modified

- `backend/app/main.py`: Fish Speech wiring, generated-audio endpoint, Twilio `<Play>` responses, and signature validation on all Twilio POST routes.
- `backend/app/adapters/twilio.py`: generated-audio TwiML helper.
- `backend/app/core/config.py`: Fish Speech settings and live-readiness requirements.
- `.env.example`: Fish Speech configuration placeholders.
- `docker-compose.yml`: Fish Speech environment propagation.
- `README.md`: local Fish Speech integration instructions.

No credential-bearing `.env` file is included in the modified project workspace or final source package.

## Runtime and audio configuration

- Fish Speech server: current `fishaudio/fish-speech` repository, API version 2.0.0 source checkout.
- Model: `fishaudio/s2-pro` / S2 Pro.
- Application API: `POST /v1/tts`.
- Telephone output: WAV, 8,000 Hz, mono, 16-bit PCM.
- Audio delivery: short-lived `GET /api/telephony/audio/{audio_id}` endpoint referenced by Twilio `<Play>`.
- Reference voice: supported through `FISH_SPEECH_REFERENCE_AUDIO` + `FISH_SPEECH_REFERENCE_TEXT`, or `FISH_SPEECH_REFERENCE_ID`; no unauthorized reference audio was added.

## Test results

| Area | Result |
|---|---:|
| Python compilation | Pass |
| Backend regression and integration tests | **84 passed** |
| Zero-error scan | Pass |
| Frontend tests | **2 passed** |
| Frontend production build | Pass |
| FastAPI local startup | Pass |
| `/health`, `/api/status`, `/demo` | Pass |
| Demo TwiML compatibility route | Pass |
| Fish adapter conversion and validation | Pass using a controlled HTTP fixture |
| Fish-to-Twilio route integration | Pass using a controlled audio fixture; emits `<Play>`, not `<Say>` |
| Missing Twilio signatures on live POST routes | Rejected with HTTP 403 |
| Fish Speech package installation | Pass after installing compiler, PortAudio, and Python 3.12 development headers |
| Official S2 Pro model download | Pass; weights downloaded successfully |
| Actual S2 Pro CPU server startup | **Failed: OOM-killed with exit 137** |
| Actual Fish-generated WAV | Not available on this machine because model loading exceeded available memory |
| Actual external Twilio call | Not attempted; requires separate authorization and a reachable public deployment |

## Hardware measurement

The execution machine has 6 CPU cores, 7.8 GiB RAM, no NVIDIA GPU/CUDA, and FFmpeg 6.1.1. Fish Speech's current documentation recommends at least 24 GB GPU memory for S2 Pro inference. The server began loading the model in CPU mode and was terminated by the operating system before becoming ready. This is a hardware limitation, not a hidden fallback: the application returns `fish_speech_unavailable` when the configured Fish service cannot produce audio.

## Security changes

All live-mode Twilio POST routes now validate `X-Twilio-Signature`. The credential-bearing `.env` from the original ZIP was removed from the modified source and is not copied into the final package. Any credentials present in the original ZIP must be rotated or revoked, especially the Twilio authentication token and any provider credentials that were active in that file.

## Remaining deployment requirement

To complete real voice-quality and real-call verification, run the supplied project and `scripts/run_fish_speech.sh` on a Linux host with a compatible GPU and at least the memory recommended by Fish Speech, expose the FastAPI audio URL over HTTPS, configure Twilio webhook URLs, use an authorized reference voice if desired, and perform a separately authorized test call. The code path and automated checks for that deployment are in place, but this CPU-only sandbox cannot truthfully claim that it generated or delivered an S2 Pro voice to a caller.
