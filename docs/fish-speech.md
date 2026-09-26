# Fish Speech reference voice

Production phone turns select an approved semantic utterance and play its immutable, validated pack asset:

> caller intent → approved prompt ID → reference-derived 8 kHz mono 16-bit WAV → Asterisk `STREAM FILE` (FastAGI) → caller reply recorded and transcribed locally

The pack is generated offline with the long-running local Fish Speech server and `fish-references/mouth-care-receptionist-reference-20260919.wav`. Phone webhooks do not synthesize arbitrary text. A missing or invalid asset fails closed (the call ends); the runtime never substitutes unrelated speech or any text-to-speech voice.

## Hardware boundary

The current Fish Speech S2 Pro documentation recommends at least **24 GB of GPU memory** for inference. The original development machine has 6 CPU cores, 7.8 GiB RAM, and no NVIDIA GPU/CUDA. S2 Pro installation and weight download succeeded there, but CPU model startup was OOM-killed with exit code 137. The application records generation latency and real-time factor rather than claiming real-time performance.

## Linux installation

From a machine with sufficient resources:

```bash
git clone https://github.com/fishaudio/fish-speech.git
cd fish-speech
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[cu129]'       # select the CUDA extra matching the machine
hf download fishaudio/s2-pro --local-dir checkpoints/s2-pro
python tools/api_server.py --listen 0.0.0.0:8080 --device cuda --half
```

For CPU-only testing, use `pip install -e '.[cpu]'` and `--device cpu`. The local API is `POST http://127.0.0.1:8080/v1/tts`. The adapter sends JSON containing `text`, `format: wav`, `streaming: false`, and optional reference audio or reference ID.

The supplied project includes `scripts/run_fish_speech.sh`, which performs install, model-download, hardware checks, and server startup after the operator supplies a Fish Speech checkout.

## Windows PowerShell

Use `scripts/run_fish_speech.ps1`. It checks Python 3.12+, Git, FFmpeg, NVIDIA availability, RAM, the Fish Speech checkout, and model weights. It defaults to CUDA and refuses a CPU S2 Pro start on a host below the documented memory threshold unless CPU testing is explicitly requested:

```powershell
 .\scripts\run_fish_speech.ps1 -Backend cuda
# Explicit CPU experiment only:
 .\scripts\run_fish_speech.ps1 -Backend cpu -AllowCpu
```

A compatible NVIDIA driver/runtime is required for the CUDA path. Native Windows is supported by this launcher; WSL is not required by the project launcher.

## Docker Compose

The `voice` Compose profile includes the official `fishaudio/fish-speech:latest` service. Model weights are mounted from `FISH_SPEECH_CHECKPOINTS_DIR` (default `./fish-checkpoints`) and reference files from `FISH_SPEECH_REFERENCES_DIR` (default `./fish-references`). The API container reaches Fish Speech at the Docker service hostname `http://fish-speech:8080`, not `127.0.0.1`:

```powershell
$env:FISH_SPEECH_ENABLED = "true"
$env:FISH_SPEECH_DEVICE = "cuda"
docker compose --profile voice up --build
```

The official container requires a compatible NVIDIA runtime for the CUDA configuration. On a CPU-only machine, use the launcher with an explicitly authorized CPU test; the current S2 Pro model may not fit in small-memory hosts.

## Application configuration

Copy `.env.example` to `.env` and set:

```dotenv
FISH_SPEECH_ENABLED=true
FISH_SPEECH_BASE_URL=http://127.0.0.1:8080
FISH_SPEECH_MODEL=fish-speech-1.5
FISH_SPEECH_OUTPUT_FORMAT=wav
FISH_SPEECH_SAMPLE_RATE=8000
FISH_SPEECH_CHANNELS=1
FISH_SPEECH_TIMEOUT_SECONDS=900
FISH_SPEECH_REFERENCE_AUDIO=fish-references/mouth-care-receptionist-reference-20260919.wav
```

When running through Compose, set `FISH_SPEECH_BASE_URL=http://fish-speech:8080`. Only use authorized reference audio and its approved transcript. `FISH_SPEECH_REFERENCE_ID` is not used by the current packaged production call path.

## Telephone conversion and delivery

The generator converts Fish output with FFmpeg to 8 kHz, mono, signed 16-bit PCM WAV and validates ASR, keywords, required phrases, acoustic metrics, and hashes. It builds and validates a complete temporary pack before swapping it into place; a failed generation or validation leaves the published pack unchanged. Each asset records its own ASR model provenance, which matters when targeted regeneration uses a smaller model. During a call Asterisk plays only assets the backend has validated (`STREAM FILE` from the pack directory, seen from WSL as `/mnt/...`); FastAPI also serves them read-only at `/api/telephony/audio/{asset_id}`. There is no text-to-speech fallback.

## Readiness and status

`/ready` reports `fish_speech.configured` separately from `fish_speech.reachable` and returns HTTP 503 in live mode when Fish Speech is unreachable, configuration is invalid, the voice pack is invalid, or call state is unavailable. `/api/telephony/preflight` independently checks Asterisk, the phone line, speech recognition and packaged audio before deriving `LIVE_CALL_ALLOWED`.

## Tests and measurements

Run `python scripts/validate_voice_pack.py` after generation; it exits nonzero unless all currently required intents pass the same validator used by FastAPI. The tests validate the offline Fish generator contract, ASR and acoustic checks, WAV format, atomic publication rollback, immutable pack checksums, semantic asset selection, and the end-to-end FastAGI conversation. Generation logs include source text length, output duration, latency, and real-time factor (`latency / audio_duration`).

On the current host, the Fish environment detects that CUDA is unavailable to its installed PyTorch build even though an NVIDIA GPU is present; Fish therefore falls back to CPU. CPU generation is slow and can exhaust memory. Use a CUDA-enabled Fish environment for full-pack regeneration; do not loosen ASR/content validation or publish a partial pack to make a phone test pass.

## Verified constrained-host model

`fishaudio/fish-speech-1.5` with the official Fish Speech `v1.5.1` server was verified on the CPU-only sandbox. It generated real speech at 44.1 kHz mono, which the project adapter converted to telephone WAV. The direct adapter run produced 4.923 seconds of 8 kHz telephone audio in 248.329 seconds (RTF 50.446). This is functional but far too slow for interactive telephony on CPU; use a GPU host for production latency.

The verified model is `fish-speech-1.5` with the Fish Speech `v1.5.1` server. The production call path uses the validated pack made from this model and the configured reference audio.

## Uploaded reference voice

The project distribution includes `fish-references/mouth-care-receptionist-reference-20260919.wav`, the configured reference recording. The offline generator sends the audio and approved transcript as Fish Speech reference conditioning when producing the packaged utterances.

Set:

```dotenv
FISH_SPEECH_REFERENCE_AUDIO=fish-references/mouth-care-receptionist-reference-20260919.wav
FISH_SPEECH_REFERENCE_TEXT=Tomorrow is holiday because of Sunday. The Sunday is because of today is Saturday. Today is Saturday is because of yesterday is Friday. Friday is because of Thursday. But I know you are not willing to listen, but you have to listen.
```

The reference conditions pack generation; phone responses are the pre-generated approved WAV assets, not per-request synthesis. Validate any regenerated pack with `python scripts/validate_voice_pack.py` before publication.
