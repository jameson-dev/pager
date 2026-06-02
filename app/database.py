"""SQLite job history."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()


class JobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with _lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at  TEXT NOT NULL,
                    capcode      TEXT NOT NULL,
                    jobtype      TEXT,
                    message      TEXT NOT NULL,
                    fields_json  TEXT NOT NULL,
                    pdf_path     TEXT,
                    printed      INTEGER NOT NULL DEFAULT 0,
                    print_error  TEXT,
                    matched_rule TEXT,
                    print_attempts INTEGER NOT NULL DEFAULT 0,
                    -- print_failed = tried to print and last attempt failed and
                    -- not yet resolved. Distinct from "printed=0 because gated off".
                    print_failed INTEGER NOT NULL DEFAULT 0,
                    is_test      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_capcode ON jobs(capcode)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_received ON jobs(received_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_failed ON jobs(print_failed)")
            # --- migrate older DBs created before these columns existed ---
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            for col, ddl in (
                ("print_attempts", "ALTER TABLE jobs ADD COLUMN print_attempts INTEGER NOT NULL DEFAULT 0"),
                ("print_failed", "ALTER TABLE jobs ADD COLUMN print_failed INTEGER NOT NULL DEFAULT 0"),
                ("is_test", "ALTER TABLE jobs ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in cols:
                    conn.execute(ddl)

    def add_job(
        self,
        *,
        received_at: datetime,
        capcode: str,
        jobtype: str | None,
        message: str,
        fields: dict,
        pdf_path: str | None,
        printed: bool,
        print_error: str | None,
        matched_rule: str | None,
        attempted_print: bool = False,
        is_test: bool = False,
    ) -> int:
        # A job is "failed" only if we actually tried to print and it errored.
        print_failed = 1 if (attempted_print and not printed) else 0
        attempts = 1 if attempted_print else 0
        with _lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs
                  (received_at, capcode, jobtype, message, fields_json,
                   pdf_path, printed, print_error, matched_rule,
                   print_attempts, print_failed, is_test)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    received_at.isoformat(timespec="seconds"),
                    capcode,
                    jobtype,
                    message,
                    json.dumps(fields),
                    pdf_path,
                    1 if printed else 0,
                    print_error,
                    matched_rule,
                    attempts,
                    print_failed,
                    1 if is_test else 0,
                ),
            )
            return cur.lastrowid

    def list_jobs(self, limit: int = 200, offset: int = 0) -> list[dict]:
        with _lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _build_filter(capcode=None, jobtype=None, q=None, date_from=None,
                      date_to=None, printed=None):
        """Build a parameterized WHERE clause from optional filters."""
        clauses, params = [], []
        if capcode:
            clauses.append("capcode = ?"); params.append(str(capcode))
        if jobtype:
            clauses.append("jobtype = ?"); params.append(jobtype)
        if q:
            clauses.append("(message LIKE ? OR fields_json LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if date_from:
            clauses.append("received_at >= ?"); params.append(date_from)
        if date_to:
            # inclusive end-of-day if a bare date was given
            clauses.append("received_at <= ?")
            params.append(date_to if len(date_to) > 10 else date_to + "T23:59:59")
        if printed == "yes":
            clauses.append("printed = 1")
        elif printed == "no":
            clauses.append("printed = 0")
        elif printed == "failed":
            clauses.append("print_failed = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def query_jobs(self, *, limit=200, offset=0, **filters) -> list[dict]:
        where, params = self._build_filter(**filters)
        with _lock, self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def count_jobs(self, **filters) -> int:
        where, params = self._build_filter(**filters)
        with _lock, self._conn() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM jobs{where}", params).fetchone()[0]

    def get_job(self, job_id: int) -> dict | None:
        with _lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def mark_printed(self, job_id: int, printed: bool, error: str | None) -> None:
        """Manual (re)print result. Clears the failed flag on success."""
        with _lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET printed = ?, print_error = ?, "
                "print_failed = ?, print_attempts = print_attempts + 1 WHERE id = ?",
                (1 if printed else 0, error, 0 if printed else 1, job_id),
            )

    def update_print_result(self, job_id: int, printed: bool, error: str | None, attempts: int) -> None:
        """Used by the retry worker; sets the absolute attempt count."""
        with _lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET printed = ?, print_error = ?, "
                "print_failed = ?, print_attempts = ? WHERE id = ?",
                (1 if printed else 0, error, 0 if printed else 1, attempts, job_id),
            )

    def list_failed_unresolved(self, max_attempts: int) -> list[dict]:
        with _lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE print_failed = 1 AND print_attempts < ? "
                "AND pdf_path IS NOT NULL ORDER BY id ASC",
                (max_attempts,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def count_failed_unresolved(self, max_attempts: int) -> int:
        with _lock, self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE print_failed = 1 AND print_attempts < ?",
                (max_attempts,),
            ).fetchone()
            return row["c"]

    def list_old_jobs(self, before_iso: str) -> list[dict]:
        """Jobs received before the cutoff, for retention cleanup."""
        with _lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE received_at < ?", (before_iso,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def delete_jobs(self, job_ids: list[int]) -> int:
        if not job_ids:
            return 0
        placeholders = ",".join("?" * len(job_ids))
        with _lock, self._conn() as conn:
            cur = conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", job_ids)
            return cur.rowcount

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["fields"] = json.loads(d.pop("fields_json", "{}"))
        d["printed"] = bool(d["printed"])
        return d
