# Fish Speech (TTS)

Fish Speech is the only speech synthesis engine. It is not the telephony provider, the database
or the speech recogniser.

## Installed version (verified)

| Item | Value |
| --- | --- |
| Checkout | `E:\fish-speech`, tag **v1.5.1** (commit `58046ea`) |
| Model | `fish-speech-1.5`: `checkpoints/fish-speech-1.5/model.pth` + `firefly-gan-vq-fsq-8x1024-21hz-generator.pth` |
| Runtime | Python 3.12.10, PyTorch 2.4.1+cpu (`torch.cuda.is_available() == False`) |
| Local patches | `tools/server/model_manager.py` (float32 on CPU, no CPU warm-up), `tools/server/views.py` (synthesis off the event loop) |
| Server | `tools/api_server.py --listen 127.0.0.1:8080 --device cpu --workers 1 --llama-checkpoint-path checkpoints/fish-speech-1.5 --decoder-checkpoint-path checkpoints/fish-speech-1.5/firefly-gan-vq-fsq-8x1024-21hz-generator.pth --decoder-config-name firefly_gan_vq` |
| Health | `GET /v1/health` → `{"status":"ok"}` (the port opens only after the model has loaded, ≈ 73 s on this PC) |
| TTS | `POST /v1/tts`, msgpack `ServeTTSRequest` |
| Output | WAV, 44 100 Hz, mono, 16-bit signed little-endian PCM |

The host has an NVIDIA RTX 2050 (4 GB), but the Fish environment uses CPU-only PyTorch, so inference
runs on the CPU. Installing a CUDA build of PyTorch 2.4.1 into `E:\fish-speech\.venv` is the first
thing to try for faster synthesis; it was not changed as part of this work.

## Reference voice

`fish-references/mouth-care-receptionist-reference-20260919.wav` — 21.93 s, 44.1 kHz, mono, 16-bit
PCM, SHA-256 `ac73a7d1…3ffa1b` (recorded in the voice-pack manifest). Transcript:
`fish-references/mouth-care-receptionist-reference.txt` (= `FISH_SPEECH_REFERENCE_TEXT`). Each
request sends the audio and transcript as `references` with `use_memory_cache=on`, so the server
encodes the reference once and reuses the tokens. `fish-references/mouth-care-receptionist-reference.wav`
is an earlier take kept for provenance; it is not used.

## How calls use Fish Speech

1. **Voice pack** (`artifacts/voice-pack`, 25 approved utterances): generated offline by
   `scripts/generate_voice_pack_http.py` from the reference voice, converted to 8 kHz telephone WAV,
   verified (hash, reference hash, acoustic speech checks, faster-whisper `small.en` transcript with
   keywords and required phrases) and published atomically. The runtime re-validates it and converts
   every asset once to telephone PCM, so the first audio of every reply is immediate.
2. **Phrase cache** (`artifacts/tts-cache`): Fish output for arbitrary text, keyed by text + voice
   signature (model, version, reference hash, reference transcript, generation parameters, seed).
   Audio made with another voice/reference/model is never served.
3. **Live synthesis** (`FISH_SPEECH_LIVE_SYNTHESIS=true`, off by default): sentence by sentence, with
   a first-audio budget; optional streaming. v1.5.1 streams **headerless** 16-bit PCM (its WAV header
   is dropped server-side), so the client uses the rate from an earlier WAV response or
   `FISH_SPEECH_SAMPLE_RATE` and otherwise refuses the stream.
4. **Failure**: the pre-generated `technical_issue` asset ("I am sorry, we are having a temporary
   technical issue. Please try again shortly.") and the call ends. No other TTS engine exists.

## Measured performance (development PC, CPU)

Host: Intel Core i5-12450H (8 cores / 12 threads), 15.7 GB RAM, Windows 11, Fish on CPU.
Reports: `diagnostics/fish_speech_performance_*.json`; voice-pack timings in `artifacts/voice-pack/manifest.json`.

| Test | Audio | Synthesis | RTF |
| --- | --- | --- | --- |
| Cold first request "Hello, thank you for calling Mouth Care Solutions." | 4.64 s | 80.0 s | 17.2 |
| Warm, same text (non-streaming) | 6.08 s | 76.1–82.5 s | 12.5–13.6 |
| Warm, same text (streaming; first audio at the end) | 6.08 s | 78.0 s | 12.8 |
| "Thank you." ×2 concurrently | 0.46 s each | 9.96 s and 19.73 s (serialised) | ≈ 21 |
| Voice pack generation, 25 utterances (typical attempts) | 3.6–17 s each | 69–244 s each | 12.3–22.7 (mean 14.5) |

While synthesizing, the Fish process keeps about 5.3 of 12 logical CPUs busy and holds 2.7 GB (3.6 GB
cold). One outlier request took 890 s for 7.3 s of audio. The faster-whisper `small.en` check hears the
static-test audio correctly ("hello thank you for calling mouth care solutions"); `base.en` misheard
"Mouth Care" as "Healthcare".

**Conclusion:** live Fish Speech synthesis on this CPU is 12–17× slower than real time and cannot
serve a phone turn. Production calls use the verified pre-generated voice pack (0 ms synthesis at
call time). Live synthesis of new sentences needs a CUDA GPU host; re-measure there with
`scripts/measure_fish_speech.py` before enabling `FISH_SPEECH_LIVE_SYNTHESIS`.

## Operations

* Start: `scripts/run_fish_speech.ps1` (installs v1.5.1 and the 1.5 weights when missing) or
  `scripts/run_local_stack.ps1`. Linux: `scripts/run_fish_speech.sh`.
* Keep the server private (loopback or private network); the application never exposes it.
* Health from the app: `GET /api/voice/fish/health` distinguishes configured, reachable (a busy
  server can time out), model_loaded and ready.
* Add or change an utterance: edit `knowledge/clinic/voice_prompts.json`, then
  `E:\fish-speech\.venv\Scripts\python.exe scripts/generate_voice_pack_http.py --only <ids> --publish`
  and `python scripts/validate_voice_pack.py`.
* Docker: the optional `fish-speech` Compose service pins `fishaudio/fish-speech:v1.5.1` for CUDA hosts.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `/v1/health` refused for ~1–2 min after start | model still loading on CPU |
| health times out while a request runs | v1.5.1 streaming blocks its event loop; the app reports `health_timeout` |
| `RuntimeError: bad allocation` in the Fish log | out of memory during decoding; close other heavy processes |
| pack `reference_audio_mismatch` | the reference WAV changed; regenerate the whole pack |
| pack `asr_*` errors | a take was misheard; regenerate that asset with `--only` |
