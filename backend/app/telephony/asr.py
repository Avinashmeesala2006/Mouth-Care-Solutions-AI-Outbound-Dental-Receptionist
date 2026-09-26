"""Local, open-source speech recognition for the phone conversation (faster-whisper).

Asterisk records each caller turn to an 8 kHz WAV; this module transcribes it on the
CPU. The model is loaded once in the background so readiness can be reported honestly
('loading', 'ready' or an error) without blocking the API.
"""
from __future__ import annotations

import threading
from pathlib import Path


class Transcriber:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.status = 'not_loaded'
        self.error: str | None = None
        self._model = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.status == 'ready'

    def load(self) -> None:
        with self._lock:
            if self._model is not None or self.status == 'loading':
                return
            self.status = 'loading'
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(self.model_name, device='cpu', compute_type='int8')
        except Exception as exc:  # import error, download failure, out of memory
            self.status, self.error = 'error', f'{type(exc).__name__}: {str(exc)[:200]}'
            return
        with self._lock:
            self._model, self.status, self.error = model, 'ready', None

    def load_in_background(self) -> None:
        threading.Thread(target=self.load, name='asr-load', daemon=True).start()

    def transcribe(self, path: Path) -> str:
        if not self._model:
            raise RuntimeError(f'asr_not_ready ({self.status})')
        segments, _ = self._model.transcribe(str(path), language='en', beam_size=5, vad_filter=True)
        return ' '.join(segment.text.strip() for segment in segments).strip()
