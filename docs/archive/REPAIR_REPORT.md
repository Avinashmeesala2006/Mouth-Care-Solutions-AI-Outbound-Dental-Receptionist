> **Historical report (superseded 2026-09-26).** Its voice-pack, preflight and call claims are not current: the previous voice pack was synthetic sine tones and has been replaced by an ASR-verified Fish reference-voice pack. See README.md and `GET /api/twilio/preflight` for the current state.

# Fish Speech Repair Pass

## Changes made

The previously integrated project was repaired without replacing the existing agent or Fish Speech adapter.

| Area | Change |
|---|---|
| Windows | Added `scripts/run_fish_speech.ps1` with Python, Git, FFmpeg, NVIDIA, RAM, model-weight, and CPU/CUDA checks, plus installation and startup logic. |
| Docker | Added a `fish-speech` Compose service using the official `fishaudio/fish-speech:latest` image under the `voice` profile. |
| Container networking | Changed the API’s Compose default Fish endpoint to `http://fish-speech:8080`; no separate-service use of `127.0.0.1`. |
| Readiness | `/ready` now distinguishes `fish_speech.configured` from `fish_speech.reachable` and returns 503 in live mode when the Fish health endpoint is unavailable. |
| Status | `/api/status` and `/ready` report `fish-speech` when enabled rather than ElevenLabs. |
| Tests | Added readiness configuration/reachability coverage and preserved all existing regression tests. |
| Documentation | Updated Fish Speech setup, PowerShell, Docker, hardware, and readiness instructions. |

## Verification

- Backend: **85 passed**
- Python compilation: passed
- Zero-error scan: passed
- Frontend: **2 passed**
- Frontend production build: passed
- Credential/artifact scan: passed after generated caches were removed
- PowerShell parser: not available in the Linux sandbox, so the `.ps1` launcher could not be executed here
- Docker Compose validation: Docker is not installed in the sandbox, so the Compose service could not be started here

## Real Fish Speech status

The previous implementation pass installed the current Fish Speech package and downloaded the official S2 Pro weights. Starting the real S2 Pro server on this machine was attempted in CPU mode and was OOM-killed with exit code 137. The machine has 7.8 GiB RAM and no GPU, while current Fish Speech guidance recommends substantially more memory/GPU capacity.

This repair pass does not claim real S2 Pro audio generation, human-like voice quality, or a real Twilio call on this host. The Windows launcher now refuses an under-sized CPU start by default, and the Docker path is configured for a compatible GPU runtime. Actual audio and Twilio verification must be performed on a suitable GPU host with public HTTPS and separately authorized provider credentials.

## Important external-action boundary

No real Twilio call was placed. A live call would require separately confirming the exact destination and accepting the external provider action immediately before it is made.

## Real Fish Speech verification completed

The official smaller model `fishaudio/fish-speech-1.5` was selected because S2 Pro was not viable on the CPU-only host. Fish Speech `v1.5.1` loaded the model and served the real `/v1/tts` endpoint. The requested receptionist phrase generated a real 44.1 kHz mono PCM WAV in 193.996 seconds; it was 3.855 seconds long, 340,012 bytes, non-silent, and unclipped. The telephone conversion produced 8 kHz mono 16-bit PCM WAV, 3.855 seconds, 61,750 bytes, also non-silent and unclipped. Speech-to-text recovered the requested sentence exactly.

The project adapter was then run against the real server and produced an 8 kHz telephone WAV in 248.329 seconds with a 4.923-second duration and RTF 50.446. Finally, the existing agent route was exercised with clinic-hours input. It completed in 271.495 seconds and returned TwiML containing `<Play>` referencing the generated audio endpoint, with no `<Say>` element. The referenced artifact was fetched from the API and transcribed as the agent's clinic-hours response.

This proves the real Fish Speech model and the existing agent-to-adapter-to-Twilio-Play path. CPU latency is not suitable for live interactive calls; a compatible GPU host is required for production responsiveness. No external Twilio call was placed because public HTTPS, valid provisioning, and a separately confirmed call destination were not available in this sandbox.

## Uploaded voice reference integration

The authorized WhatsApp recording was extracted to `fish-references/mouth-care-receptionist-reference.wav` as mono 44.1 kHz PCM WAV. It is approximately 21.95 seconds, has no detected clipping, and is paired with the supplied transcript in `FISH_SPEECH_REFERENCE_TEXT`. The existing adapter now sends the file as a base64 `references` item to Fish Speech; it does not use the recording as prerecorded response audio.

A real Fish Speech v1.5.1 request using that reference voice generated the receptionist sentence through the project adapter. Metrics: 7.384-second telephone WAV, 118,222 bytes, 8 kHz mono 16-bit PCM, 389.671 seconds generation time, RTF 52.772. Transcription recovered: "Hi. Thank you for calling Mouth Care Solutions. How can I help you today?" The output is saved under `artifacts/fish-speech-1.5-uploaded-voice-telephone.wav`.

Waveform and transcription checks passed. Subjective voice-identity similarity cannot be reliably certified by an automated waveform/transcription check in this environment; the generated sample is included for human listening. CPU latency remains unsuitable for live phone conversations, so a GPU host is required for practical deployment.

## Final uploaded-reference live-agent route

The existing `/api/telephony/twilio/voice` route was run with Fish Speech enabled, the uploaded reference WAV configured, and the real Fish Speech v1.5.1 server active. The agent response was dynamically synthesized using the reference audio. The route completed in 370.429 seconds and returned TwiML with `<Play>http://127.0.0.1:8012/api/telephony/audio/89771cccbd754da8a0b76fb06117cc55</Play>` and no `<Say>`. The fetched artifact was 8 kHz mono 16-bit PCM WAV, 111 KB, and transcribed as the generated clinic-hours response. This proves the existing agent path is configured to deliver generated Fish Speech audio conditioned on the uploaded voice reference.

A real external Twilio call was not placed because this sandbox has no public HTTPS webhook and no separately confirmed call destination. CPU generation latency is 370.429 seconds for the agent route; production requires a compatible GPU host. Subjective voice similarity remains a human-listening judgment; the generated reference-conditioned samples and route artifact are included for that evaluation.

## Default configuration

The intended live configuration now defaults to `FISH_SPEECH_ENABLED=true` in `.env.example` and Docker Compose, with `FISH_SPEECH_REFERENCE_AUDIO=fish-references/mouth-care-receptionist-reference.wav` and the matching reference transcript. Environment variables remain overridable. The demo test environment continues to use its isolated mock settings when no live `.env` is supplied.
