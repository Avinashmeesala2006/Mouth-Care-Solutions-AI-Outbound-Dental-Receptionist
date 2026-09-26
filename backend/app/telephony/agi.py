"""FastAGI server that runs the receptionist conversation on an answered Asterisk call.

Dialplan hands the answered channel to ``agi://<host>:<port>/receptionist`` with the
application call id and direction as arguments. For every turn the server:

1. plays the phone agent's prompts with ``STREAM FILE`` from the verified Fish voice pack
   (only assets that passed validation; a missing asset ends the call - no other voice);
2. records the caller with ``RECORD FILE`` (silence detection) into a directory shared
   with this host, transcribes it locally with faster-whisper and deletes the audio;
3. feeds the transcript to the grounded phone agent and persists state and evidence.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import wave
from pathlib import Path

from ..services import phone_agent

logger = logging.getLogger(__name__)
_SAFE_NAME = re.compile(r'[^A-Za-z0-9_-]')
_RESULT = re.compile(r'result=(-?\d+)')
_ENDPOS = re.compile(r'endpos=(\d+)')


class ChannelHungUp(Exception):
    pass


class AGIChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, timeout: float = 60.0):
        self.reader, self.writer, self.timeout = reader, writer, timeout
        self.hung_up = False

    async def read_env(self) -> dict:
        env = {}
        while True:
            line = (await asyncio.wait_for(self.reader.readline(), self.timeout)).decode('utf-8', 'replace').strip()
            if not line:
                return env
            if ': ' in line:
                key, value = line.split(': ', 1)
                env[key] = value

    async def command(self, text: str, *, timeout: float | None = None) -> str:
        if self.hung_up:
            raise ChannelHungUp()
        self.writer.write((text + '\n').encode())
        await self.writer.drain()
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), timeout or self.timeout)
            if not raw:
                self.hung_up = True
                raise ChannelHungUp()
            line = raw.decode('utf-8', 'replace').strip()
            if line == 'HANGUP':
                self.hung_up = True
                continue
            if line.startswith('511'):
                self.hung_up = True
                raise ChannelHungUp()
            if line[:3].isdigit():
                return line


class ReceptionistAGI:
    def __init__(self, settings, store, voice_pack_fn, transcriber):
        self.settings = settings
        self.store = store
        self.voice_pack_fn = voice_pack_fn
        self.transcriber = transcriber

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel = AGIChannel(reader, writer)
        call_id = 'unknown'
        try:
            env = await channel.read_env()
            call_id = env.get('agi_arg_1') or env.get('agi_uniqueid') or 'unknown'
            direction = 'outbound' if env.get('agi_arg_2') == 'outbound' else 'inbound'
            await self.converse(channel, call_id, direction, env)
        except ChannelHungUp:
            self.store.add_event(call_id, 'agi_caller_hung_up', {})
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            self.store.add_event(call_id, 'agi_connection_error', {'error': type(exc).__name__})
        except Exception as exc:  # never leave the caller on a dead line without evidence
            logger.exception('agi_session_failed call_id=%s', call_id)
            self.store.add_event(call_id, 'agi_session_failed', {'error': type(exc).__name__})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _caller(self, call_id: str, direction: str, env: dict) -> str:
        if direction == 'outbound':
            request = self.store.get_call_request(call_id)
            return (request or {}).get('destination', '')
        return env.get('agi_callerid', '')

    async def converse(self, channel: AGIChannel, call_id: str, direction: str, env: dict) -> None:
        state = self.store.load_session(call_id) or phone_agent.new_session(direction)
        self.store.save_session(call_id, state)
        self.store.add_event(call_id, 'agi_session_started', {'direction': direction, 'script': env.get('agi_network_script')})
        caller = self._caller(call_id, direction, env)
        prompts, hangup, turn_index = [phone_agent.greeting_prompt(direction)], False, 0
        while True:
            if not await self.play(channel, call_id, prompts):
                await self._hangup(channel, call_id, 'voice_asset_unavailable')
                return
            if hangup:
                await self._hangup(channel, call_id, 'conversation_complete')
                return
            turn_index += 1
            speech = await self.listen(channel, call_id, turn_index)
            turn = phone_agent.respond(state, speech, max_turns=self.settings.max_call_turns)
            state = turn.state
            self.store.save_session(call_id, state)
            if turn.appointment:
                record = self.store.add_appointment_request(call_id=call_id, caller=caller, details=turn.appointment)
                self.store.add_event(call_id, 'appointment_request_recorded', {'id': record['id']})
            if turn.callback_topic:
                record = self.store.add_callback(source='phone', contact=caller, topic=turn.callback_topic,
                                                 preferred_window='To be arranged', call_id=call_id)
                self.store.add_event(call_id, 'callback_recorded', {'id': record['id']})
            self.store.add_event(call_id, 'speech_turn', {'turn': turn_index, 'speech': speech, 'intent': turn.intent,
                                                          'prompts': turn.prompts, 'hangup': turn.hangup,
                                                          'stage': state['stage']})
            prompts, hangup = turn.prompts, turn.hangup

    async def play(self, channel: AGIChannel, call_id: str, prompts: list[str]) -> bool:
        pack = self.voice_pack_fn()
        missing = [p for p in prompts if p not in pack.assets]
        if missing or not prompts:
            self.store.add_event(call_id, 'voice_asset_unavailable', {'missing': missing, 'prompts': prompts})
            return False
        voice_dir = self.settings.asterisk_voice_path()
        for asset_id in prompts:
            asset = pack.assets[asset_id]
            reply = await channel.command(f'STREAM FILE "{voice_dir}/{asset_id}" ""', timeout=asset.duration_seconds + 30)
            result = _RESULT.search(reply)
            endpos = int(_ENDPOS.search(reply).group(1)) if _ENDPOS.search(reply) else 0
            played = bool(result) and result.group(1) != '-1' and endpos > 0
            self.store.add_event(call_id, 'audio_played' if played else 'audio_play_failed',
                                 {'asset': asset_id, 'sha256': asset.sha256, 'endpos_samples': endpos, 'reply': reply[:80]})
            if not played:
                if channel.hung_up or (result and result.group(1) == '-1' and endpos > 0):
                    raise ChannelHungUp()
                return False
        return True

    async def listen(self, channel: AGIChannel, call_id: str, turn_index: int) -> str:
        name = f'{_SAFE_NAME.sub("_", call_id)}-{turn_index:02d}'
        remote = f'{self.settings.asterisk_recording_dir.rstrip("/")}/{name}'
        timeout_ms = self.settings.asr_record_timeout_seconds * 1000
        reply = await channel.command(f'RECORD FILE "{remote}" wav "#" {timeout_ms} 0 s={self.settings.asr_silence_seconds}',
                                      timeout=self.settings.asr_record_timeout_seconds + 30)
        if '(hangup)' in reply or channel.hung_up:
            raise ChannelHungUp()
        local = self.settings.local_recording_path() / f'{name}.wav'
        text = await asyncio.get_running_loop().run_in_executor(None, self._transcribe_and_delete, call_id, local)
        return text

    def _transcribe_and_delete(self, call_id: str, local: Path) -> str:
        deadline = time.monotonic() + 3.0
        while not local.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not local.is_file():
            self.store.add_event(call_id, 'recording_missing', {'file': local.name})
            return ''
        started = time.perf_counter()
        try:
            with wave.open(str(local), 'rb') as wav_file:
                seconds = wav_file.getnframes() / max(1, wav_file.getframerate())
            text = self.transcriber.transcribe(local) if seconds >= 0.3 else ''
        except Exception as exc:
            self.store.add_event(call_id, 'asr_failed', {'error': type(exc).__name__})
            text = ''
        finally:
            try:
                local.unlink()  # caller audio is not retained
            except OSError:
                pass
        self.store.add_event(call_id, 'asr_transcribed', {'chars': len(text), 'seconds': round(time.perf_counter() - started, 2)})
        return text

    async def _hangup(self, channel: AGIChannel, call_id: str, reason: str) -> None:
        self.store.add_event(call_id, 'agi_hangup', {'reason': reason})
        try:
            await channel.command('HANGUP', timeout=5)
        except (ChannelHungUp, asyncio.TimeoutError, ConnectionError, OSError):
            pass


class AGIServer:
    """Runs the FastAGI listener on its own event loop thread."""

    def __init__(self, handler: ReceptionistAGI, host: str, port: int):
        self.handler, self.host, self.port = handler, host, port
        self.status = 'stopped'
        self.error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    @property
    def listening(self) -> bool:
        return self.status == 'listening'

    def start(self, wait: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name='fastagi', daemon=True)
        self._thread.start()
        self._started.wait(wait)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._server = self._loop.run_until_complete(asyncio.start_server(self.handler.handle, self.host, self.port))
            self.port = self._server.sockets[0].getsockname()[1]
            self.status = 'listening'
        except OSError as exc:
            self.status, self.error = 'error', f'{type(exc).__name__}: {exc}'
            self._started.set()
            return
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._server.close()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()
            self.status = 'stopped'

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
