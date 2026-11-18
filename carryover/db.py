"""SQLite helpers and schema for the carryover review app."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    high_threshold  REAL NOT NULL CHECK (high_threshold >= 0),
    blank_limit     REAL NOT NULL CHECK (blank_limit >= 0),
    data_version    INTEGER NOT NULL DEFAULT 0,
    result_state    TEXT NOT NULL DEFAULT 'pending'
                    CHECK (result_state IN ('pending', 'current', 'stale')),
    result_version  INTEGER,
    result_error    TEXT,
    computed_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injections (
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    injection_no    TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('sample', 'blank')),
    peak_area       REAL CHECK (peak_area IS NULL OR peak_area >= 0),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    position        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (batch_id, injection_no),
    CHECK (kind = 'blank' OR peak_area IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS injection_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    injection_no    TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('sample', 'blank')),
    peak_area       REAL CHECK (peak_area IS NULL OR peak_area >= 0),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    accepted_at     TEXT NOT NULL,
    UNIQUE (batch_id, injection_no, revision)
);

CREATE TABLE IF NOT EXISTS results (
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    injection_no    TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    peak_area       REAL,
    revision        INTEGER NOT NULL,
    status          TEXT NOT NULL,
    trigger_injection TEXT,
    note            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, injection_no)
);

CREATE TABLE IF NOT EXISTS upload_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    accepted_count  INTEGER NOT NULL,
    skipped_count   INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_batch
    ON injection_revisions(batch_id, injection_no, revision);
CREATE INDEX IF NOT EXISTS idx_upload_log_batch
    ON upload_log(batch_id, id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open an autocommit connection; transactions are started explicitly."""
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """Read several tables from one consistent SQLite snapshot."""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


@contextmanager
def immediate(conn: sqlite3.Connection):
    """Serialize a read/write unit across threads and processes."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection, attempts: int = 5) -> None:
    # Several gunicorn workers may boot at once; switching journal mode needs
    # a brief exclusive lock, so retry instead of failing startup.
    for attempt in range(attempts):
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    conn.executescript(SCHEMA)

    # A crash or a failed recompute can leave a batch whose stored rows are
    # older than its accepted revisions.  Never let that combination appear
    # as a current result, even before the next page request.
    with immediate(conn):
        conn.execute(
            """
            UPDATE batches
               SET result_state = 'stale',
                   result_version = NULL,
                   result_error = COALESCE(
                       result_error,
                       '服务重启后发现结果版本与数据版本不一致，已作废'
                   )
             WHERE result_state = 'current'
               AND (result_version IS NULL OR result_version != data_version)
            """
        )
        conn.execute(
            """
            DELETE FROM results
             WHERE batch_id IN (
                 SELECT id FROM batches
                  WHERE result_state != 'current'
                     OR result_version IS NULL
                     OR result_version != data_version
             )
            """
        )
