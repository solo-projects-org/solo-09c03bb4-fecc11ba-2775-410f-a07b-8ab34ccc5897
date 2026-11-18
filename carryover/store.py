"""Transactional persistence and result-version checks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import engine
from .db import immediate, snapshot
from .engine import Injection, ResultRow
from .ingest import ParsedRow


PENDING = "pending"
CURRENT = "current"
STALE = "stale"


class StoreError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class UploadSummary:
    accepted: int
    skipped: int
    data_version: int
    changed: bool


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.*,
               (SELECT COUNT(*) FROM injections i WHERE i.batch_id = b.id) AS injection_count,
               (SELECT COUNT(*) FROM results r WHERE r.batch_id = b.id) AS result_count
          FROM batches b
         ORDER BY b.id DESC
        """
    ).fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _insert_revision(
    conn: sqlite3.Connection,
    batch_id: int,
    row: ParsedRow,
    accepted_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO injection_revisions
            (batch_id, injection_no, kind, peak_area, revision, accepted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (batch_id, row.injection_no, row.kind, row.peak_area, row.revision, accepted_at),
    )


def _upsert_injection(
    conn: sqlite3.Connection,
    batch_id: int,
    row: ParsedRow,
    updated_at: str,
    position: int,
) -> None:
    conn.execute(
        """
        INSERT INTO injections
            (batch_id, injection_no, kind, peak_area, revision, position, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id, injection_no) DO UPDATE SET
            kind = excluded.kind,
            peak_area = excluded.peak_area,
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        (
            batch_id, row.injection_no, row.kind, row.peak_area,
            row.revision, position, updated_at,
        ),
    )


def _log_upload(
    conn: sqlite3.Connection,
    batch_id: int,
    filename: str,
    accepted: int,
    skipped: int,
) -> None:
    conn.execute(
        """
        INSERT INTO upload_log (batch_id, filename, accepted_count, skipped_count, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (batch_id, filename[:255], accepted, skipped, utcnow()),
    )


def create_batch(
    conn: sqlite3.Connection,
    name: str,
    high_threshold: float,
    blank_limit: float,
    rows: list[ParsedRow],
    filename: str,
) -> int:
    now = utcnow()
    with immediate(conn):
        cursor = conn.execute(
            """
            INSERT INTO batches
                (name, high_threshold, blank_limit, data_version, result_state,
                 created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (name, high_threshold, blank_limit, PENDING, now, now),
        )
        batch_id = int(cursor.lastrowid)
        for position, row in enumerate(rows, start=1):
            _insert_revision(conn, batch_id, row, now)
            _upsert_injection(conn, batch_id, row, now, position)
        conn.execute(
            "UPDATE batches SET data_version = 1, updated_at = ? WHERE id = ?",
            (now, batch_id),
        )
        _log_upload(conn, batch_id, filename, len(rows), 0)
    return batch_id


def accept_upload(
    conn: sqlite3.Connection,
    batch_id: int,
    rows: Iterable[ParsedRow],
    filename: str,
) -> UploadSummary:
    rows = list(rows)
    now = utcnow()
    with immediate(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            raise StoreError("批次不存在")

        existing = {
            row["injection_no"]: row
            for row in conn.execute(
                """
                SELECT injection_no, kind, peak_area, revision, position
                  FROM injections WHERE batch_id = ?
                """,
                (batch_id,),
            )
        }
        next_position = max(
            (row["position"] for row in existing.values()), default=0
        ) + 1

        accepted: list[tuple[ParsedRow, int]] = []
        skipped = 0
        for row in rows:
            current = existing.get(row.injection_no)
            if current is not None:
                if row.revision <= current["revision"]:
                    # Same number accepts only a strictly higher revision.
                    skipped += 1
                    continue
                position = current["position"]
            else:
                # A number seen for the first time keeps the file order and is
                # appended after the injections already in the batch.
                position = next_position
                next_position += 1
            accepted.append((row, position))
            existing[row.injection_no] = {
                "injection_no": row.injection_no,
                "kind": row.kind,
                "peak_area": row.peak_area,
                "revision": row.revision,
                "position": position,
            }

        data_version = batch["data_version"]
        if accepted:
            for row, position in accepted:
                _insert_revision(conn, batch_id, row, now)
                _upsert_injection(conn, batch_id, row, now, position)
            data_version += 1
            # The old result is invalid the moment a correction is accepted,
            # before any recompute is attempted.
            conn.execute("DELETE FROM results WHERE batch_id = ?", (batch_id,))
            conn.execute(
                """
                UPDATE batches
                   SET data_version = ?, result_state = ?, result_version = NULL,
                       result_error = NULL, computed_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (data_version, PENDING, now, batch_id),
            )

        _log_upload(conn, batch_id, filename, len(accepted), skipped)

    return UploadSummary(
        accepted=len(accepted),
        skipped=skipped,
        data_version=data_version,
        changed=bool(accepted),
    )


def recompute(conn: sqlite3.Connection, batch_id: int) -> list[ResultRow]:
    with immediate(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            raise StoreError("批次不存在")
        injections = [
            Injection(
                injection_no=row["injection_no"],
                kind=row["kind"],
                peak_area=row["peak_area"],
                revision=row["revision"],
                position=row["position"],
            )
            for row in conn.execute(
                """
                SELECT injection_no, kind, peak_area, revision, position
                  FROM injections WHERE batch_id = ?
                 ORDER BY position, injection_no
                """,
                (batch_id,),
            )
        ]
        results = engine.compute_results(
            injections, batch["high_threshold"], batch["blank_limit"]
        )
        # The write and the version stamp live in the same IMMEDIATE
        # transaction as the read, so a concurrent correction cannot be
        # interleaved and produce a mixture of revisions.
        snapshot_version = batch["data_version"]
        conn.execute("DELETE FROM results WHERE batch_id = ?", (batch_id,))
        for seq, result in enumerate(results, start=1):
            conn.execute(
                """
                INSERT INTO results
                    (batch_id, injection_no, seq, kind, peak_area, revision,
                     status, trigger_injection, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    result.injection_no,
                    seq,
                    result.kind,
                    result.peak_area,
                    result.revision,
                    result.status,
                    result.trigger_injection,
                    result.note,
                ),
            )
        cursor = conn.execute(
            """
            UPDATE batches
               SET result_state = ?, result_version = ?, result_error = NULL,
                   computed_at = ?, updated_at = ?
             WHERE id = ? AND data_version = ?
            """,
            (CURRENT, snapshot_version, utcnow(), utcnow(), batch_id, snapshot_version),
        )
        if cursor.rowcount != 1:  # pragma: no cover - defensive only
            raise StoreError("结果版本更新失败")
    return results


def mark_failed(conn: sqlite3.Connection, batch_id: int, message: str) -> None:
    """Never leave old rows behind when a recompute cannot be completed.

    The write is conditional: if another request already produced a current
    result for the latest data version, this failure record must not clobber
    it.
    """
    now = utcnow()
    with immediate(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return
        if (
            batch["result_state"] == CURRENT
            and batch["result_version"] is not None
            and batch["result_version"] == batch["data_version"]
        ):
            return
        conn.execute("DELETE FROM results WHERE batch_id = ?", (batch_id,))
        conn.execute(
            """
            UPDATE batches
               SET result_state = ?, result_version = NULL,
                   result_error = ?, updated_at = ?
             WHERE id = ?
            """,
            (STALE, message[:2000], now, batch_id),
        )


def inspect_results(
    conn: sqlite3.Connection, batch_id: int
) -> tuple[Optional[sqlite3.Row], Optional[list[sqlite3.Row]]]:
    """Return (batch, current results or None), repairing a forged "current".

    Reads happen in a deferred (WAL snapshot) transaction so page views do not
    serialize against each other.  Only when the snapshot looks inconsistent is
    a write transaction taken, and the check is repeated there so a concurrent
    recompute that already committed is never invalidated from stale data.
    """
    with snapshot(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return None, None
        if (
            batch["result_state"] != CURRENT
            or batch["result_version"] is None
            or batch["result_version"] != batch["data_version"]
        ):
            return batch, None
        results = conn.execute(
            "SELECT * FROM results WHERE batch_id = ? ORDER BY seq", (batch_id,)
        ).fetchall()
        if _snapshot_is_consistent(conn, batch, results):
            return batch, results

    with immediate(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return None, None
        if (
            batch["result_state"] != CURRENT
            or batch["result_version"] is None
            or batch["result_version"] != batch["data_version"]
        ):
            return batch, None
        results = conn.execute(
            "SELECT * FROM results WHERE batch_id = ? ORDER BY seq", (batch_id,)
        ).fetchall()
        if not _snapshot_is_consistent(conn, batch, results):
            conn.execute("DELETE FROM results WHERE batch_id = ?", (batch_id,))
            conn.execute(
                """
                UPDATE batches
                   SET result_state = ?, result_version = NULL,
                       result_error = ?, updated_at = ?
                 WHERE id = ?
                """,
                (STALE, "现行结果与已受理修订不一致，已作废", utcnow(), batch_id),
            )
            return get_batch(conn, batch_id), None
        return batch, results


def _snapshot_is_consistent(
    conn: sqlite3.Connection, batch: sqlite3.Row, results: list[sqlite3.Row]
) -> bool:
    injections = conn.execute(
        """
        SELECT injection_no, kind, peak_area, revision
          FROM injections WHERE batch_id = ? ORDER BY position, injection_no
        """,
        (batch["id"],),
    ).fetchall()
    if len(results) != len(injections):
        return False
    by_no = {row["injection_no"]: row for row in results}
    for injection in injections:
        result = by_no.get(injection["injection_no"])
        if result is None:
            return False
        if (
            result["revision"] != injection["revision"]
            or result["kind"] != injection["kind"]
            or result["peak_area"] != injection["peak_area"]
        ):
            return False
    return True




def get_revisions(
    conn: sqlite3.Connection, batch_id: int, limit: int = 200
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM injection_revisions
         WHERE batch_id = ? ORDER BY id DESC LIMIT ?
        """,
        (batch_id, limit),
    ).fetchall()


def get_upload_log(
    conn: sqlite3.Connection, batch_id: int, limit: int = 30
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM upload_log
         WHERE batch_id = ? ORDER BY id DESC LIMIT ?
        """,
        (batch_id, limit),
    ).fetchall()
