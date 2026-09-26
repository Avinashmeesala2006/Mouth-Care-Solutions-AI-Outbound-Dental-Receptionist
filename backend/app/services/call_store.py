"""Durable call state for the phone receptionist (SQLite, standard library only).

A phone call spans an Asterisk origination, asynchronous AMI call events and a
FastAGI conversation, so per-call conversation state, call records, status history
and the appointment/callback requests captured on the phone must survive process
restarts. PostgreSQL is not available on this host, so this store keeps that state
in a local SQLite file.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_requests (
    request_id TEXT PRIMARY KEY,
    call_id TEXT UNIQUE,
    name TEXT NOT NULL,
    destination TEXT NOT NULL,
    preferred_window TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_requests_destination_idx ON call_requests(destination, created_at);
CREATE TABLE IF NOT EXISTS call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_events_call_idx ON call_events(call_id, id);
CREATE TABLE IF NOT EXISTS call_sessions (
    call_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointment_requests (
    id TEXT PRIMARY KEY,
    call_id TEXT,
    caller TEXT,
    patient_name TEXT,
    preferred_date TEXT,
    preferred_time TEXT,
    reason TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS callback_requests (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    call_id TEXT,
    contact TEXT NOT NULL,
    topic TEXT NOT NULL,
    preferred_window TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ':memory:':
                self._conn.execute('PRAGMA journal_mode=WAL')
            self._migrate_legacy_columns()
            self._conn.executescript(SCHEMA)

    def _migrate_legacy_columns(self) -> None:
        """Rename the legacy provider column (call_sid) to call_id in existing databases."""
        for table in ('call_requests', 'call_events', 'call_sessions', 'appointment_requests', 'callback_requests'):
            columns = {row[1] for row in self._conn.execute(f'PRAGMA table_info({table})').fetchall()}
            if 'call_sid' in columns and 'call_id' not in columns:
                self._conn.execute(f'ALTER TABLE {table} RENAME COLUMN call_sid TO call_id')
        self._conn.execute('DROP INDEX IF EXISTS call_events_sid_idx')

    def _execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    def healthy(self) -> bool:
        try:
            with self._lock:
                self._conn.execute('BEGIN')
                self._conn.execute("INSERT INTO call_events(call_id, kind, data, at) VALUES (NULL, 'health_probe', '{}', ?)", (_now(),))
                self._conn.execute('ROLLBACK')
            return True
        except sqlite3.Error:
            return False

    # Call requests -------------------------------------------------------------
    def create_call_request(self, *, name: str, destination: str, preferred_window: str, topic: str, mode: str) -> dict:
        now = _now()
        request_id = f"CR-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4).upper()}"
        self._execute(
            'INSERT INTO call_requests(request_id, name, destination, preferred_window, topic, status, mode, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (request_id, name, destination, preferred_window, topic, 'pending', mode, now, now),
        )
        return self.get_call_request(request_id)

    def update_call_request(self, request_id: str, **fields) -> dict | None:
        allowed = {'call_id', 'status', 'mode', 'error'}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            assignments = ', '.join(f'{key} = ?' for key in fields)
            self._execute(f'UPDATE call_requests SET {assignments}, updated_at = ? WHERE request_id = ?',
                          (*fields.values(), _now(), request_id))
        return self.get_call_request(request_id)

    def update_status_by_call_id(self, call_id: str, status: str) -> bool:
        cursor = self._execute('UPDATE call_requests SET status = ?, updated_at = ? WHERE call_id = ?', (status, _now(), call_id))
        return cursor.rowcount > 0

    def get_call_request(self, request_id: str) -> dict | None:
        rows = self._rows('SELECT * FROM call_requests WHERE request_id = ?', (request_id,))
        return rows[0] if rows else None

    def get_call_request_by_call_id(self, call_id: str) -> dict | None:
        rows = self._rows('SELECT * FROM call_requests WHERE call_id = ?', (call_id,))
        return rows[0] if rows else None

    def recent_live_calls(self, destination: str, seconds: int) -> int:
        since = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        rows = self._rows(
            "SELECT COUNT(*) AS n FROM call_requests WHERE destination = ? AND mode = 'live' AND created_at >= ? "
            "AND (call_id IS NOT NULL OR status IN ('pending', 'creating'))",
            (destination, since),
        )
        return rows[0]['n']

    # Events ------------------------------------------------------------------
    def add_event(self, call_id: str | None, kind: str, data: dict | None = None) -> None:
        self._execute('INSERT INTO call_events(call_id, kind, data, at) VALUES (?, ?, ?, ?)',
                      (call_id, kind, json.dumps(data or {}, sort_keys=True, default=str), _now()))

    def events(self, call_id: str | None = None, limit: int = 200) -> list[dict]:
        if call_id:
            rows = self._rows('SELECT * FROM call_events WHERE call_id = ? ORDER BY id DESC LIMIT ?', (call_id, limit))
        else:
            rows = self._rows("SELECT * FROM call_events WHERE kind != 'health_probe' ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row['data'] = json.loads(row['data'])
        return list(reversed(rows))

    # Conversation sessions ---------------------------------------------------
    def load_session(self, call_id: str) -> dict | None:
        rows = self._rows('SELECT state FROM call_sessions WHERE call_id = ?', (call_id,))
        return json.loads(rows[0]['state']) if rows else None

    def save_session(self, call_id: str, state: dict) -> None:
        self._execute(
            'INSERT INTO call_sessions(call_id, state, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(call_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at',
            (call_id, json.dumps(state, sort_keys=True), _now()),
        )

    # Requests captured by the receptionist -----------------------------------
    def add_appointment_request(self, *, call_id: str, caller: str, details: dict) -> dict:
        record = {
            'id': f'AR-{secrets.token_hex(4).upper()}', 'call_id': call_id, 'caller': caller,
            'patient_name': details.get('patient_name', ''), 'preferred_date': details.get('preferred_date', ''),
            'preferred_time': details.get('preferred_time', ''), 'reason': details.get('reason', ''),
            'status': 'requested', 'created_at': _now(),
        }
        self._execute(
            'INSERT INTO appointment_requests(id, call_id, caller, patient_name, preferred_date, preferred_time, reason, status, created_at) '
            'VALUES (:id, :call_id, :caller, :patient_name, :preferred_date, :preferred_time, :reason, :status, :created_at)',
            record,
        )
        return record

    def add_callback(self, *, source: str, contact: str, topic: str, preferred_window: str, call_id: str | None = None) -> dict:
        record = {
            'id': f'CB-{secrets.token_hex(4).upper()}', 'source': source, 'call_id': call_id, 'contact': contact,
            'topic': topic, 'preferred_window': preferred_window, 'status': 'queued', 'created_at': _now(),
        }
        self._execute(
            'INSERT INTO callback_requests(id, source, call_id, contact, topic, preferred_window, status, created_at) '
            'VALUES (:id, :source, :call_id, :contact, :topic, :preferred_window, :status, :created_at)',
            record,
        )
        return record

    def appointment_requests(self, limit: int = 100) -> list[dict]:
        return self._rows('SELECT * FROM appointment_requests ORDER BY created_at DESC LIMIT ?', (limit,))

    def callbacks(self, limit: int = 100) -> list[dict]:
        return self._rows('SELECT * FROM callback_requests ORDER BY created_at DESC LIMIT ?', (limit,))
