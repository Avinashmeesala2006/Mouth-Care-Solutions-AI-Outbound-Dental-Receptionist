"""The receptionist conversation over FastAGI, driven by a protocol-faithful fake Asterisk."""
import pytest

from backend.app.core.config import Settings
from backend.app.services.call_store import CallStore
from backend.app.telephony.agi import AGIServer, ReceptionistAGI
from fake_asterisk import FakeAsteriskCall
from test_receptionist_core import fake_pack

DEST = '+919900000001'


class ScriptedTranscriber:
    """Stands in for faster-whisper: returns the caller's lines in order."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.heard_files = []
        self.status = 'ready'
        self.ready = True

    def transcribe(self, path):
        self.heard_files.append(path.name)
        return self.lines.pop(0) if self.lines else ''


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, asterisk_voice_dir='/mnt/e/project/artifacts/voice-pack',
                        asterisk_recording_dir='/var/spool/asterisk/mouthcare-recordings', local_recording_dir=str(tmp_path))
    store = CallStore(':memory:')
    servers = []

    def start(lines, pack=None):
        transcriber = ScriptedTranscriber(lines)
        server = AGIServer(ReceptionistAGI(settings, store, lambda: pack or fake_pack(), transcriber), '127.0.0.1', 0)
        server.start()
        assert server.listening, server.error
        servers.append(server)
        return server, transcriber

    yield settings, store, tmp_path, start
    for server in servers:
        server.stop()


def outbound_request(store):
    request = store.create_call_request(name='t', destination=DEST, preferred_window='Now', topic='t', mode='live')
    store.update_call_request(request['request_id'], call_id=request['request_id'], status='answered')
    return request['request_id']


def test_full_outbound_call_plays_fish_voice_records_speech_and_hangs_up(setup):
    settings, store, recordings, start = setup
    call_id = outbound_request(store)
    server, transcriber = start(['I want to book an appointment', 'tomorrow', '10 am', 'Meena', 'check up', 'yes', 'no thanks'])
    fake = FakeAsteriskCall(server.port, recordings, call_id=call_id)
    commands = fake.run()
    assert fake.played == ['outbound_greeting', 'appointment_request', 'ask_preferred_time', 'ask_patient_name',
                           'ask_reason_for_visit', 'confirm_appointment', 'appointment_recorded', 'anything_else', 'goodbye']
    assert commands[0] == 'STREAM FILE "/mnt/e/project/artifacts/voice-pack/outbound_greeting" ""'
    assert commands[-1] == 'HANGUP'
    assert all('SAY' not in c.upper().split()[0] for c in commands)
    record = [c for c in commands if c.startswith('RECORD FILE')][0]
    assert record == f'RECORD FILE "/var/spool/asterisk/mouthcare-recordings/{call_id}-01" wav "#" 10000 0 s=2'
    assert list(recordings.iterdir()) == []  # caller audio is deleted after transcription
    assert transcriber.heard_files == [f'{call_id}-{n:02d}.wav' for n in range(1, 8)]
    [appointment] = store.appointment_requests()
    assert appointment['caller'] == DEST and appointment['patient_name'] == 'Meena' and appointment['status'] == 'requested'
    kinds = [e['kind'] for e in store.events(call_id)]
    assert kinds.count('audio_played') == 9 and kinds.count('speech_turn') == 7
    assert kinds[0] == 'agi_session_started' and kinds[-1] == 'agi_hangup'
    played = [e['data'] for e in store.events(call_id) if e['kind'] == 'audio_played']
    assert all(d['endpos_samples'] > 0 and len(d['sha256']) == 64 for d in played)


def test_caller_hanging_up_mid_call_is_recorded(setup):
    settings, store, recordings, start = setup
    call_id = outbound_request(store)
    server, _ = start(['what are your hours'])
    fake = FakeAsteriskCall(server.port, recordings, call_id=call_id, caller_hangs_up_after_turns=1)
    commands = fake.run()
    assert fake.played == ['outbound_greeting', 'clinic_hours', 'anything_else']
    assert 'HANGUP' not in commands
    assert store.events(call_id)[-1]['kind'] == 'agi_caller_hung_up'


def test_inbound_call_greets_and_records_callbacks_for_the_caller(setup):
    settings, store, recordings, start = setup
    server, _ = start(['How much does whitening cost?', 'bye'])
    fake = FakeAsteriskCall(server.port, recordings, call_id='', direction='inbound')
    fake.run()
    assert fake.played == ['greeting', 'unsupported_question', 'anything_else', 'goodbye']
    [callback] = store.callbacks()
    assert callback['source'] == 'phone' and callback['contact'] == DEST and 'whitening' in callback['topic']


def test_missing_verified_asset_ends_the_call_without_any_other_voice(setup):
    settings, store, recordings, start = setup
    call_id = outbound_request(store)
    server, _ = start([], pack=fake_pack(['greeting', 'goodbye']))
    fake = FakeAsteriskCall(server.port, recordings, call_id=call_id)
    commands = fake.run()
    assert fake.played == [] and commands == ['HANGUP']
    assert store.events(call_id)[1]['kind'] == 'voice_asset_unavailable'


def test_asterisk_failing_to_play_a_file_ends_the_call(setup):
    settings, store, recordings, start = setup
    call_id = outbound_request(store)
    server, _ = start([])
    fake = FakeAsteriskCall(server.port, recordings, call_id=call_id, missing_files={'outbound_greeting'})
    commands = fake.run()
    assert commands[-1] == 'HANGUP'
    assert any(e['kind'] == 'audio_play_failed' for e in store.events(call_id))


def test_silence_twice_then_goodbye(setup):
    settings, store, recordings, start = setup
    call_id = outbound_request(store)
    server, _ = start(['', '', ''])
    fake = FakeAsteriskCall(server.port, recordings, call_id=call_id)
    fake.run()
    assert fake.played == ['outbound_greeting', 'repeat_or_not_understood', 'repeat_or_not_understood', 'goodbye']
