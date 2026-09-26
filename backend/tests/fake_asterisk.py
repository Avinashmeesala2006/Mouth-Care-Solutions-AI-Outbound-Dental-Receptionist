"""Test doubles that speak the real Asterisk wire protocols (AMI server, FastAGI client)."""
from __future__ import annotations

import io
import math
import random
import socket
import socketserver
import threading
import wave


def _block(fields: dict) -> bytes:
    lines = []
    for key, value in fields.items():
        for item in (value if isinstance(value, list) else [value]):
            lines.append(f'{key}: {item}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode()


class FakeAMI:
    """Threaded AMI server. ``state`` controls what Asterisk 'has' (dialplan, trunk, modem...)."""

    def __init__(self, **state):
        self.state = {'username': 'mouthcare', 'secret': 'test-secret', 'version': '20.6.0',
                      'contexts': {'mouthcare-receptionist'}, 'modules': {'format_wav'},
                      'registrations': [], 'endpoints': set(), 'commands': {}, 'originate_response': 'Success',
                      **state}
        self.actions: list[dict] = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'Asterisk Call Manager/9.0.0\r\n')
                buffer = b''
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    while b'\r\n\r\n' in buffer:
                        raw, buffer = buffer.split(b'\r\n\r\n', 1)
                        fields = {}
                        for line in raw.decode().split('\r\n'):
                            key, _, value = line.partition(': ')
                            fields.setdefault(key, []).append(value)
                        action = {k: v if len(v) > 1 else v[0] for k, v in fields.items()}
                        fake.actions.append(action)
                        if not fake.respond(self.wfile, action):
                            return

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, out, action: dict) -> bool:
        name, aid, s = action.get('Action'), action.get('ActionID'), self.state
        ok = {'Response': 'Success', 'ActionID': aid}
        if name == 'Login':
            if action.get('Username') == s['username'] and action.get('Secret') == s['secret']:
                out.write(_block({**ok, 'Message': 'Authentication accepted'}))
            else:
                out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': 'Authentication failed'}))
            return True
        if name == 'Logoff':
            out.write(_block({'Response': 'Goodbye', 'ActionID': aid}))
            return False
        if name == 'CoreSettings':
            out.write(_block({**ok, 'AsteriskVersion': s['version']}))
        elif name == 'ShowDialPlan':
            if action.get('Context') in s['contexts']:
                out.write(_block({**ok, 'EventList': 'start', 'Message': 'DialPlan list will follow'}))
                out.write(_block({'Event': 'ListDialplan', 'ActionID': aid, 'Context': action['Context'], 'Extension': 's', 'Priority': '1'}))
                out.write(_block({'Event': 'ShowDialPlanComplete', 'ActionID': aid, 'EventList': 'Complete'}))
            else:
                out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': 'Did not find context'}))
        elif name == 'ModuleCheck':
            if action.get('Module') in s['modules']:
                out.write(_block({**ok, 'Version': ''}))
            else:
                out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': 'Module not loaded'}))
        elif name == 'PJSIPShowRegistrationsOutbound':
            out.write(_block({**ok, 'EventList': 'start'}))
            for reg in s['registrations']:
                out.write(_block({'Event': 'OutboundRegistrationDetail', 'ActionID': aid, **reg}))
            out.write(_block({'Event': 'OutboundRegistrationDetailComplete', 'ActionID': aid, 'EventList': 'Complete'}))
        elif name == 'PJSIPShowEndpoint':
            if action.get('Endpoint') in s['endpoints']:
                out.write(_block({**ok, 'Message': 'Following are Events for each object'}))
            else:
                out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': 'Unable to retrieve endpoint'}))
        elif name == 'Command':
            output = s['commands'].get(action.get('Command'), 'No such command')
            out.write(_block({**ok, 'Output': output.splitlines() or ['']}))
        elif name == 'Originate':
            if s['originate_response'] == 'Success':
                out.write(_block({**ok, 'Message': 'Originate successfully queued'}))
            else:
                out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': s['originate_response']}))
        elif name == 'Hangup':
            out.write(_block({**ok, 'Message': 'Channel Hungup'}))
        else:
            out.write(_block({'Response': 'Error', 'ActionID': aid, 'Message': 'Invalid/unknown command'}))
        return True


def speech_wav(seconds: float = 1.5, rate: int = 8000, seed: int = 3) -> bytes:
    rng = random.Random(seed)
    frames = bytearray()
    for i in range(int(seconds * rate)):
        t = i / rate
        envelope = max(0.0, math.sin(2 * math.pi * 3.1 * t))
        value = int(9000 * envelope * (0.6 * math.sin(2 * math.pi * 180 * t) + 0.4 * rng.uniform(-1, 1)))
        frames += max(-32767, min(32767, value)).to_bytes(2, 'little', signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return buffer.getvalue()


class FakeAsteriskCall:
    """Plays the Asterisk side of a FastAGI session against the application's AGI server."""

    def __init__(self, port: int, recording_dir, *, call_id: str = '', direction: str = 'outbound',
                 caller_hangs_up_after_turns: int | None = None, missing_files: set[str] | None = None):
        self.port, self.recording_dir, self.call_id, self.direction = port, recording_dir, call_id, direction
        self.hangup_after = caller_hangs_up_after_turns
        self.missing_files = missing_files or set()
        self.commands: list[str] = []
        self.played: list[str] = []
        self.recordings = 0

    def run(self, timeout: float = 20.0) -> list[str]:
        with socket.create_connection(('127.0.0.1', self.port), timeout=timeout) as sock:
            stream = sock.makefile('rwb')
            env = {'agi_network': 'yes', 'agi_network_script': 'receptionist', 'agi_channel': 'PJSIP/pstn-trunk-00000001',
                   'agi_uniqueid': self.call_id or '1790000000.1', 'agi_callerid': '+919900000001',
                   'agi_arg_1': self.call_id, 'agi_arg_2': self.direction}
            for key, value in env.items():
                stream.write(f'{key}: {value}\n'.encode())
            stream.write(b'\n')
            stream.flush()
            while True:
                line = stream.readline().decode().strip()
                if not line:
                    return self.commands
                self.commands.append(line)
                if line.startswith('STREAM FILE'):
                    path = line.split('"')[1]
                    name = path.rsplit('/', 1)[-1]
                    if name in self.missing_files:
                        stream.write(b'200 result=-1 endpos=0\n')
                    else:
                        self.played.append(name)
                        stream.write(b'200 result=0 endpos=12000\n')
                elif line.startswith('RECORD FILE'):
                    if self.hangup_after is not None and self.recordings >= self.hangup_after:
                        stream.write(b'HANGUP\n200 result=-1 (hangup) endpos=0\n')
                        stream.flush()
                        continue
                    self.recordings += 1
                    remote = line.split('"')[1]
                    (self.recording_dir / (remote.rsplit('/', 1)[-1] + '.wav')).write_bytes(speech_wav())
                    stream.write(b'200 result=0 (timeout) endpos=12000\n')
                elif line == 'HANGUP':
                    stream.write(b'200 result=1\n')
                    stream.flush()
                    return self.commands
                else:
                    stream.write(b'510 Invalid or unknown command\n')
                stream.flush()
