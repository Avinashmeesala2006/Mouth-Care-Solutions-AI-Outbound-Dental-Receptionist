"""Ordered SQL migrations (``migrations/NNN_name.sql``) tracked in ``schema_migrations``.

Each file is applied once, in its own transaction, under an advisory lock so concurrent
starters cannot race. Applied files are checksummed (line endings normalised): editing an
applied migration is reported as an error instead of being silently re-run.

    python -m backend.app.db.migrations            # apply pending migrations
    python -m backend.app.db.migrations --status   # list applied / pending
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from ..core.config import MIGRATIONS_DIR

_LOCK_ID = 7_401_202_609  # arbitrary, stable advisory-lock key for this application


class MigrationError(RuntimeError):
    pass


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str, str]]:
    """[(version, checksum, sql)] sorted by file name."""
    result = []
    for path in sorted(directory.glob('*.sql')):
        sql = path.read_text(encoding='utf-8').replace('\r\n', '\n')
        normalized = '\n'.join(line.rstrip() for line in sql.strip().splitlines())
        result.append((path.stem, hashlib.sha256(normalized.encode('utf-8')).hexdigest(), sql))
    return result


def _ensure_table(conn) -> None:
    conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, '
                 'applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')


def status(conn, directory: Path = MIGRATIONS_DIR) -> dict:
    from psycopg.rows import tuple_row

    cur = conn.cursor(row_factory=tuple_row)   # independent of the connection's row factory
    exists = cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()[0]
    applied = dict(cur.execute('SELECT version, checksum FROM schema_migrations').fetchall()) if exists else {}
    files = migration_files(directory)
    return {
        'applied': [v for v, _, _ in files if v in applied],
        'pending': [v for v, _, _ in files if v not in applied],
        'modified': [v for v, checksum, _ in files if v in applied and applied[v] != checksum],
        'unknown': sorted(set(applied) - {v for v, _, _ in files}),
    }


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    import psycopg

    applied_now = []
    with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute('SELECT pg_advisory_lock(%s)', (_LOCK_ID,))
        try:
            _ensure_table(conn)
            state = status(conn, directory)
            if state['modified']:
                raise MigrationError('applied migrations were modified: ' + ', '.join(state['modified']))
            for version, checksum, sql in migration_files(directory):
                if version not in state['pending']:
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute('INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)', (version, checksum))
                applied_now.append(version)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (_LOCK_ID,))
    return applied_now


def main() -> int:
    from ..core.config import settings

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--database-url', default=None, help='defaults to DATABASE_URL')
    args = parser.parse_args()
    url = args.database_url or settings.database_url
    if not url:
        print('DATABASE_URL is not set', file=sys.stderr)
        return 2
    if args.status:
        import psycopg
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            print(status(conn))
        return 0
    applied = migrate(url)
    print('applied: ' + (', '.join(applied) if applied else 'nothing (up to date)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
