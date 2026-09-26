"""Minimal Asterisk Manager Interface (AMI) client.

AMI is a line protocol over TCP: every message is a block of ``Key: Value`` lines
terminated by a blank line. Actions carry an ``ActionID`` that the matching
``Response`` (and, for list actions, the following ``EventList`` events) echo back.
The connection is kept on localhost (manager.conf binds 127.0.0.1) and the secret is
never logged or returned.
"""
from __future__ import annotations

import itertools
import socket
import threading
import time

_ids = itertools.count(1)


class AMIError(RuntimeError):
    """AMI transport or protocol failure (message never contains the secret)."""


def parse_message(block: str) -> dict:
    """Parse one AMI message. Repeated keys (``Output``, ``Variable``) become lists."""
    message: dict = {}
    for line in block.split('\r\n'):
        if ': ' in line:
            key, value = line.split(': ', 1)
        elif line.endswith(':'):
            key, value = line[:-1], ''
        else:
            continue
        if key in message:
            existing = message[key]
            message[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            message[key] = value
    return message


def encode_action(action: str, fields: dict) -> bytes:
    lines = [f'Action: {action}']
    for key, value in fields.items():
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            text = str(item)
            if '\r' in text or '\n' in text:
                raise AMIError(f'invalid newline in AMI field {key}')
            lines.append(f'{key}: {text}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode()


class AMIConnection:
    def __init__(self, host: str, port: int, username: str, secret: str, *, timeout: float = 5.0, events: bool = False):
        self.host, self.port, self.username = host, port, username
        self._secret = secret
        self.timeout = timeout
        self.events = events
        self.banner = ''
        self._sock: socket.socket | None = None
        self._buffer = b''
        self._lock = threading.Lock()

    # Transport -----------------------------------------------------------------------
    def connect(self) -> 'AMIConnection':
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
            self.banner = self._read_line()
        except OSError as exc:
            self.close()
            raise AMIError(f'ami_unreachable {self.host}:{self.port} ({type(exc).__name__})') from exc
        if not self.banner.startswith('Asterisk Call Manager'):
            self.close()
            raise AMIError('ami_unexpected_banner')
        response = self.action('Login', Username=self.username, Secret=self._secret, Events='on' if self.events else 'off')
        if response.get('Response') != 'Success':
            self.close()
            raise AMIError('ami_login_failed: ' + str(response.get('Message', 'authentication rejected')))
        return self

    def set_timeout(self, seconds: float) -> None:
        self.timeout = seconds
        if self._sock:
            self._sock.settimeout(seconds)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        try:
            if self._sock:
                self._send(encode_action('Logoff', {'ActionID': f'mcs-{next(_ids)}'}))
        except (OSError, AMIError):
            pass
        self.close()

    def _send(self, payload: bytes) -> None:
        if not self._sock:
            raise AMIError('ami_not_connected')
        try:
            self._sock.sendall(payload)
        except OSError as exc:
            raise AMIError(f'ami_send_failed ({type(exc).__name__})') from exc

    def _fill(self) -> None:
        try:
            chunk = self._sock.recv(65536) if self._sock else b''
        except socket.timeout as exc:
            raise AMIError('ami_timeout') from exc
        except OSError as exc:
            raise AMIError(f'ami_recv_failed ({type(exc).__name__})') from exc
        if not chunk:
            raise AMIError('ami_connection_closed')
        self._buffer += chunk

    def _read_line(self) -> str:
        while b'\r\n' not in self._buffer:
            self._fill()
        line, self._buffer = self._buffer.split(b'\r\n', 1)
        return line.decode('utf-8', 'replace')

    def read_message(self) -> dict:
        while b'\r\n\r\n' not in self._buffer:
            self._fill()
        block, self._buffer = self._buffer.split(b'\r\n\r\n', 1)
        text = block.decode('utf-8', 'replace')
        if 'Response: Follows' in text and '--END COMMAND--' not in text:
            # Legacy Command output: raw lines until the end marker.
            while b'--END COMMAND--' not in self._buffer:
                self._fill()
            rest, self._buffer = self._buffer.split(b'--END COMMAND--', 1)
            self._buffer = self._buffer.lstrip(b'\r\n')
            text += '\r\n' + '\r\n'.join('Output: ' + line for line in rest.decode('utf-8', 'replace').splitlines())
        return parse_message(text)

    # Actions ----------------------------------------------------------------------------
    def action(self, name: str, **fields) -> dict:
        """Send an action and return its Response (events for other ActionIDs are skipped)."""
        with self._lock:
            action_id = fields.pop('ActionID', None) or f'mcs-{next(_ids)}'
            self._send(encode_action(name, {**fields, 'ActionID': action_id}))
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                message = self.read_message()
                if 'Response' in message and message.get('ActionID') == action_id:
                    return message
            raise AMIError(f'ami_no_response {name}')

    def list_action(self, name: str, **fields) -> tuple[dict, list[dict]]:
        """Send a list action; return (response, events) up to the EventList completion."""
        with self._lock:
            action_id = f'mcs-{next(_ids)}'
            self._send(encode_action(name, {**fields, 'ActionID': action_id}))
            response, events = None, []
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                message = self.read_message()
                if message.get('ActionID') != action_id:
                    continue
                if 'Response' in message:
                    response = message
                    if message.get('Response') != 'Success' or message.get('EventList') != 'start':
                        return response, events
                    continue
                if message.get('EventList') == 'Complete':
                    return response or {}, events
                events.append(message)
            raise AMIError(f'ami_list_incomplete {name}')

    def command(self, cli: str) -> str:
        response = self.action('Command', Command=cli)
        if response.get('Response') not in {'Success', 'Follows'}:
            raise AMIError('ami_command_failed: ' + str(response.get('Message', cli)))
        output = response.get('Output', [])
        return '\n'.join(output if isinstance(output, list) else [output])
