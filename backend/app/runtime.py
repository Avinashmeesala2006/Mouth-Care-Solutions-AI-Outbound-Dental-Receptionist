"""Application runtime: builds every component from settings, owns their lifecycle."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from .core.config import VOICE_PROMPTS_PATH, Settings
from .db.base import CallRepository
from .db.memory import InMemoryCallRepository
from .services.voice_pack import VoicePackStatus, load_prompts, load_voice_pack
from .telephony.twilio import TwilioTelephony
from .voice.audio_adapter import l16
from .voice.conversation import GroundedReceptionistEngine
from .voice.fish_speech import FishHealth, FishParams, FishSpeechClient
from .voice.tts import FishSpeechTTS, PhraseCache

logger = logging.getLogger(__name__)
FISH_HEALTH_TTL_SECONDS = 20.0


def build_repository(settings: Settings) -> CallRepository:
    if settings.database_url:
        from .db.postgres import PostgresCallRepository
        return PostgresCallRepository(settings.database_url, min_size=settings.database_pool_min_size,
                                      max_size=settings.database_pool_max_size)
    return InMemoryCallRepository()


class Runtime:
    def __init__(self, settings: Settings, *, repo: CallRepository | None = None):
        self.settings = settings
        self.repo = repo or build_repository(settings)
        self.prompts = load_prompts(VOICE_PROMPTS_PATH)
        self._pack_lock = threading.Lock()
        self._pack_cache: dict = {'signature': None, 'status': None}
        self.fish = FishSpeechClient(
            base_url=settings.fish_speech_base_url, enabled=settings.fish_speech_enabled, api_key=settings.fish_speech_api_key,
            timeout_seconds=settings.fish_speech_timeout_seconds,
            reference_audio=(settings.project_path(settings.fish_speech_reference_audio)
                             if settings.fish_speech_reference_audio else None),
            reference_text=settings.fish_speech_reference_text, reference_id=settings.fish_speech_reference_id,
            params=FishParams(chunk_length=settings.fish_speech_chunk_length, max_new_tokens=settings.fish_speech_max_new_tokens,
                              top_p=settings.fish_speech_top_p, repetition_penalty=settings.fish_speech_repetition_penalty,
                              temperature=settings.fish_speech_temperature, seed=settings.fish_speech_seed),
            model=settings.fish_speech_model, version=settings.fish_speech_version,
            expected_sample_rate=settings.fish_speech_sample_rate, max_concurrency=settings.fish_speech_max_concurrent_requests)
        self.stream_format = l16(8000)
        self.tts = FishSpeechTTS(client=self.fish, voice_pack=self.voice_pack,
                                 cache=PhraseCache(settings.project_path(settings.fish_speech_cache_dir)),
                                 target=self.stream_format, live_synthesis=settings.fish_speech_live_synthesis,
                                 streaming=settings.fish_speech_streaming,
                                 first_audio_budget_seconds=settings.fish_speech_live_first_audio_budget_seconds)
        self.engine = GroundedReceptionistEngine(self.prompts, max_turns=settings.max_call_turns)
        self.telephony = TwilioTelephony(settings, self.repo, readiness=self.voice_blockers, asset_url=self.public_asset_url)
        self.instance_id: str | None = None   # set by the app; used to prove the public origin reaches this process
        self._fish_health: tuple[float, FishHealth] | None = None
        self.started = False

    # Voice pack ------------------------------------------------------------------------------------
    def _pack_signature(self) -> tuple:
        pack_dir = self.settings.project_path(self.settings.fish_speech_voice_pack_dir)
        reference = self.settings.project_path(self.settings.fish_speech_reference_audio)
        paths = [VOICE_PROMPTS_PATH, reference, *(sorted(pack_dir.iterdir()) if pack_dir.is_dir() else [])]
        items: list[tuple[str, int | None, int | None]] = []
        for path in paths:
            try:
                stat = path.stat()
                items.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                items.append((str(path), None, None))
        return tuple(items)

    def voice_pack(self) -> VoicePackStatus:
        """Validated voice pack, reloaded whenever a pack, prompt or reference file changes."""
        signature = self._pack_signature()
        with self._pack_lock:
            if self._pack_cache['signature'] != signature:
                self.prompts = load_prompts(VOICE_PROMPTS_PATH)
                status = load_voice_pack(self.settings.project_path(self.settings.fish_speech_voice_pack_dir),
                                         self.prompts, self.settings.project_path(self.settings.fish_speech_reference_audio))
                self._pack_cache.update(signature=signature, status=status)
                self.engine = GroundedReceptionistEngine(self.prompts, max_turns=self.settings.max_call_turns)
                logger.info('voice_pack_loaded valid=%s verified=%d/%d errors=%s', status.valid, len(status.assets),
                            len(status.required), status.errors[:8])
            return self._pack_cache['status']

    def public_asset_url(self, asset_id: str) -> str | None:
        origin = self.settings.resolve().public_origin
        if not origin or asset_id not in self.voice_pack().assets:
            return None
        return f'{origin}/api/telephony/audio/{asset_id}'

    # Readiness ----------------------------------------------------------------------------------------
    def voice_blockers(self) -> list[str]:
        """What prevents the voice pipeline from holding a conversation right now."""
        blockers = []
        pack = self.voice_pack()
        if not pack.valid:
            blockers.append('VOICE_PACK_INVALID: ' + ', '.join(pack.errors[:4]))
        if not self.repo.healthy():
            blockers.append('DATABASE_UNAVAILABLE')
        elif self.repo.migration_status().get('pending'):
            blockers.append('DATABASE_MIGRATIONS_PENDING')
        if self.settings.fish_speech_live_synthesis:
            health = self.cached_fish_health()
            if health is None or not health.ready:
                blockers.append('FISH_SPEECH_NOT_READY (live synthesis enabled)')
        return blockers

    def cached_fish_health(self) -> FishHealth | None:
        return self._fish_health[1] if self._fish_health else None

    async def fish_health(self, *, max_age: float = FISH_HEALTH_TTL_SECONDS) -> FishHealth:
        now = time.monotonic()
        if self._fish_health and now - self._fish_health[0] < max_age:
            return self._fish_health[1]
        health = await self.fish.health()
        self._fish_health = (time.monotonic(), health)
        return health

    # Lifecycle -------------------------------------------------------------------------------------------
    async def startup(self) -> None:
        if self.started:
            return
        if hasattr(self.repo, 'open'):
            try:
                await asyncio.to_thread(self.repo.open, True)
            except Exception as exc:  # readiness reports it; the process still starts
                logger.error('database_open_failed error=%s', type(exc).__name__)
        if self.settings.database_url and self.settings.database_auto_migrate:
            from .db.migrations import migrate
            try:
                applied = await asyncio.to_thread(migrate, self.settings.database_url)
                logger.info('database_migrations_applied %s', applied or 'none')
            except Exception as exc:
                logger.error('database_migration_failed error=%s', type(exc).__name__)
        warmed = await asyncio.to_thread(self.tts.warm)
        cfg = self.settings.resolve()
        logger.info('runtime_started mode=%s twilio=%s db=%s voice_assets=%d app=%s', cfg.app_mode,
                cfg.twilio_enabled, self.repo.backend, warmed, self.settings.app_name)
        for error in cfg.errors:
            logger.error('config_error %s', error)
        for warning in cfg.warnings:
            logger.warning('config_warning %s', warning)
        self.started = True

    async def shutdown(self) -> None:
        if hasattr(self.repo, 'close'):
            await asyncio.to_thread(self.repo.close)
        self.started = False
