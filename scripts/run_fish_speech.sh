#!/usr/bin/env bash
set -euo pipefail
FISH_DIR="${FISH_SPEECH_DIR:-$HOME/fish-speech}"
FISH_MODEL="${FISH_SPEECH_MODEL:-fish-speech-1.5}"
FISH_REF="${FISH_SPEECH_GIT_REF:-v1.5.1}"
FISH_MODE="${FISH_SPEECH_BACKEND:-cpu}"
FISH_HOST="${FISH_SPEECH_HOST:-127.0.0.1}"
FISH_PORT="${FISH_SPEECH_PORT:-8080}"
FISH_DEVICE="${FISH_SPEECH_DEVICE:-$FISH_MODE}"
if [[ ! -d "$FISH_DIR" ]]; then
  git clone --depth 1 --branch "$FISH_REF" https://github.com/fishaudio/fish-speech "$FISH_DIR"
fi
if [[ ! -f "$FISH_DIR/tools/api_server.py" ]]; then
  echo "Fish Speech checkout not found at $FISH_DIR" >&2
  exit 1
fi
cd "$FISH_DIR"
if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
  .venv/bin/pip install -e ".[stable]"
fi
if [[ "$FISH_MODEL" == "fish-speech-1.5" ]]; then
  MODEL_DIR="checkpoints/fish-speech-1.5"
  REPO="fishaudio/fish-speech-1.5"
  if [[ ! -f "$MODEL_DIR/model.pth" ]]; then .venv/bin/hf download "$REPO" --local-dir "$MODEL_DIR"; fi
  exec .venv/bin/python tools/api_server.py --listen "${FISH_HOST}:${FISH_PORT}" --device "$FISH_DEVICE" --workers 1 --llama-checkpoint-path "$MODEL_DIR" --decoder-checkpoint-path "$MODEL_DIR/firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
else
  MODEL_DIR="checkpoints/s2-pro"
  if [[ ! -f "$MODEL_DIR/codec.pth" ]]; then .venv/bin/hf download fishaudio/s2-pro --local-dir "$MODEL_DIR"; fi
  args=(tools/api_server.py --listen "${FISH_HOST}:${FISH_PORT}" --device "$FISH_DEVICE" --workers 1)
  [[ "$FISH_DEVICE" == "cuda" ]] && args+=(--half)
  exec .venv/bin/python "${args[@]}"
fi
