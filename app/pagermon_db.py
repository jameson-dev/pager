"""Read pages directly from a PagerMon SQLite database.

An alternative ingest source to tailing the multimon log: instead of watching
`multimon.log`, poll PagerMon's own `messages.db` and feed each new row through
the same processing pipeline (rules -> PDF -> print) as the log watcher.

The whole point of this module is **tolerating schema variation**. Across
PagerMon versions and forks the `messages` table differs in ways that break a
naive ``SELECT address, message, timestamp FROM messages``:

  * timestamp stored in **seconds**, **milliseconds**, or an ISO/text string;
  * the friendly label living either in an ``alias`` column on ``messages`` or
    in a separate ``capcodes`` table joined via ``alias_id``;
  * the capcode column named ``address`` (standard) or ``capcode`` / ``cap``;
  * the body column named ``message`` (standard) or ``text`` / ``body``;
  * optional ``source`` / ``function`` columns present or absent;
  * the table itself named ``messages`` (standard) — verified, not assumed.

`probe_schema()` inspects the actual file with ``PRAGMA table_info`` and
``sqlite_master`` and returns a `DbMapping` describing how to read it. The
mapping is auto-detected but every field can be overridden from config
(Settings -> PagerMon DB), so an unrecognised fork can still be wired up by hand.

The connection is always opened **read-only** (``mode=ro``) so a live PagerMon
writer is never disturbed, and rows are fetched incrementally by ``id`` so each
page is processed exactly once.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .parser import RawPage

log = logging.getLogger("pager.pagermon_db")

# Candidate column names, in preference order, for each logical field. The first
# one that actually exists in the table wins (unless overridden in config).
_CAPCODE_CANDIDATES = ("address", "capcode", "cap", "ric", "channel")
_MESSAGE_CANDIDATES = ("message", "text", "body", "msg", "alpha")
_TIMESTAMP_CANDIDATES = ("timestamp", "time", "datetime", "received_at", "date")
_ALIAS_TEXT_CANDIDATES = ("alias", "label", "name")          # label held on messages
_ALIAS_FK_CANDIDATES = ("alias_id", "capcode_id", "cap_id")  # FK into capcodes
_FUNCTION_CANDIDATES = ("function", "func", "fn")
_TABLE_CANDIDATES = ("messages", "message", "pages", "msg")


@dataclass
class DbMapping:
    """How to read one specific PagerMon database file."""
    table: str = "messages"
    id_col: str = "id"
    capcode_col: str = "address"
    message_col: str = "message"
    timestamp_col: str | None = "timestamp"
    timestamp_kind: str = "auto"  # auto | seconds | millis | iso
    function_col: str | None = None
    # Label resolution: either a text column on the row, or a FK we LEFT JOIN to
    # a capcodes table. At most one is used; FK takes precedence when present.
    alias_text_col: str | None = None
    alias_fk_col: str | None = None
    capcodes_table: str | None = None
    capcodes_id_col: str = "id"
    capcodes_label_col: str = "alias"
    # Diagnostics filled in by probe_schema (not used to query).
    detected: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return []
    return [r[1] for r in rows]  # r[1] = column name


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return [r[0] for r in rows]


def _pick(candidates, available: list[str]) -> str | None:
    """First candidate present in `available` (case-insensitive)."""
    low = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def open_ro(db_path: str) -> sqlite3.Connection:
    """Open the PagerMon DB strictly read-only so a live writer is never blocked."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    # Read-uncommitted lets us read while PagerMon holds a write lock in WAL/journal.
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA read_uncommitted = ON")
    except sqlite3.Error:
        pass
    return conn


def probe_schema(db_path: str, override: dict | None = None) -> DbMapping:
    """Inspect `db_path` and return a `DbMapping`, applying any `override` keys.

    Never raises for a missing/odd file — returns a mapping with ``detected=False``
    and a human ``note`` explaining what was (not) found, so the UI can show it.
    """
    override = {k: v for k, v in (override or {}).items() if v not in (None, "")}
    m = DbMapping()

    p = Path(db_path)
    if not db_path or not p.exists():
        m.note = "Database file not found."
        return _apply_override(m, override)

    try:
        conn = open_ro(db_path)
    except sqlite3.Error as exc:
        m.note = f"Could not open database: {exc}"
        return _apply_override(m, override)

    try:
        tables = _tables(conn)
        table = override.get("table") or _pick(_TABLE_CANDIDATES, tables)
        if not table:
            m.note = f"No messages-like table found (tables: {', '.join(tables) or 'none'})."
            return _apply_override(m, override)
        m.table = table

        cols = _columns(conn, table)
        if not cols:
            m.note = f"Table '{table}' has no readable columns."
            return _apply_override(m, override)

        m.id_col = _pick(("id", "rowid", "_id"), cols) or "rowid"
        m.capcode_col = _pick(_CAPCODE_CANDIDATES, cols) or cols[0]
        m.message_col = _pick(_MESSAGE_CANDIDATES, cols) or cols[-1]
        m.timestamp_col = _pick(_TIMESTAMP_CANDIDATES, cols)
        m.function_col = _pick(_FUNCTION_CANDIDATES, cols)

        # Label: prefer a text column on the row; otherwise a FK into capcodes.
        m.alias_text_col = _pick(_ALIAS_TEXT_CANDIDATES, cols)
        if not m.alias_text_col:
            fk = _pick(_ALIAS_FK_CANDIDATES, cols)
            cap_tbl = _pick(("capcodes", "capcode", "aliases", "alias"), tables)
            if fk and cap_tbl:
                cap_cols = _columns(conn, cap_tbl)
                label = _pick(("alias", "label", "name", "agency"), cap_cols)
                cap_id = _pick(("id", "rowid", "_id"), cap_cols) or "id"
                if label:
                    m.alias_fk_col = fk
                    m.capcodes_table = cap_tbl
                    m.capcodes_id_col = cap_id
                    m.capcodes_label_col = label

        m.timestamp_kind = (override.get("timestamp_kind")
                            or _sniff_timestamp_kind(conn, table, m.timestamp_col))
        m.detected = True
        label_src = (m.alias_fk_col and f"JOIN {m.capcodes_table}.{m.capcodes_label_col}") \
            or m.alias_text_col or "none"
        m.note = (f"table={m.table} capcode={m.capcode_col} message={m.message_col} "
                  f"timestamp={m.timestamp_col or '-'}({m.timestamp_kind}) label={label_src}")
        return _apply_override(m, override)
    finally:
        conn.close()


def _apply_override(m: DbMapping, override: dict) -> DbMapping:
    for k, v in override.items():
        if hasattr(m, k):
            setattr(m, k, v)
    return m


def _sniff_timestamp_kind(conn, table: str, ts_col: str | None) -> str:
    """Guess seconds / millis / iso from a sample value's type and magnitude."""
    if not ts_col:
        return "auto"
    try:
        row = conn.execute(
            f'SELECT "{ts_col}" AS t FROM "{table}" '
            f'WHERE "{ts_col}" IS NOT NULL ORDER BY rowid DESC LIMIT 1'
        ).fetchone()
    except sqlite3.Error:
        return "auto"
    if not row or row["t"] is None:
        return "auto"
    val = row["t"]
    if isinstance(val, str) and not val.strip().isdigit():
        return "iso"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return "iso"
    # Epoch seconds for "now" are ~1.7e9 (10 digits); millis ~1.7e12 (13 digits).
    return "millis" if n > 1e11 else "seconds"


def _to_datetime(val, kind: str) -> datetime:
    """Normalise a PagerMon timestamp to a naive local datetime.

    `received_at` elsewhere in the app is naive local time, so we convert epoch
    values (which are UTC) to local before dropping the tzinfo, keeping displayed
    times correct.
    """
    if val is None:
        return datetime.now()
    if kind in ("seconds", "millis", "auto") and not (
        isinstance(val, str) and not val.strip().lstrip("-").isdigit()
    ):
        try:
            n = float(val)
            if kind == "millis" or (kind == "auto" and n > 1e11):
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc).astimezone().replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    # ISO / text fallback.
    s = str(val).strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,):
        try:
            dt = parse(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return datetime.now()


def _build_select(m: DbMapping) -> str:
    """SELECT for rows with id > :last_id, only touching columns that exist."""
    t = m.table
    parts = [f'm."{m.id_col}" AS _id',
             f'm."{m.capcode_col}" AS capcode',
             f'm."{m.message_col}" AS message']
    parts.append(f'm."{m.timestamp_col}" AS ts' if m.timestamp_col else "NULL AS ts")
    parts.append(f'm."{m.function_col}" AS func' if m.function_col else "NULL AS func")
    join = ""
    if m.alias_fk_col and m.capcodes_table:
        parts.append(f'c."{m.capcodes_label_col}" AS alias')
        join = (f' LEFT JOIN "{m.capcodes_table}" c '
                f'ON c."{m.capcodes_id_col}" = m."{m.alias_fk_col}"')
    elif m.alias_text_col:
        parts.append(f'm."{m.alias_text_col}" AS alias')
    else:
        parts.append("NULL AS alias")
    return (f'SELECT {", ".join(parts)} FROM "{t}" m{join} '
            f'WHERE m."{m.id_col}" > :last_id ORDER BY m."{m.id_col}" ASC LIMIT 500')


def latest_id(db_path: str, m: DbMapping) -> int:
    """Highest existing id, so a fresh start ingests only pages from now on."""
    try:
        conn = open_ro(db_path)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute(f'SELECT MAX("{m.id_col}") AS mx FROM "{m.table}"').fetchone()
        return int(row["mx"]) if row and row["mx"] is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def fetch_new(db_path: str, m: DbMapping, last_id: int) -> tuple[list[tuple[int, RawPage, str | None]], int]:
    """Return [(row_id, RawPage, alias), ...] for rows with id > last_id, plus the
    new high-water id. Alias is the resolved label (or None)."""
    sql = _build_select(m)
    try:
        conn = open_ro(db_path)
    except sqlite3.Error as exc:
        log.warning("PagerMon DB open failed: %s", exc)
        return [], last_id
    out: list[tuple[int, RawPage, str | None]] = []
    high = last_id
    try:
        for row in conn.execute(sql, {"last_id": last_id}):
            rid = int(row["_id"])
            high = max(high, rid)
            msg = (row["message"] or "").strip()
            cap = str(row["capcode"] or "").strip()
            if not cap or not msg:
                continue
            when = _to_datetime(row["ts"], m.timestamp_kind)
            func = row["func"]
            page = RawPage(capcode=cap,
                           function=str(func) if func is not None else None,
                           message=msg, proto="PAGERMON_DB", received_at=when)
            alias = row["alias"]
            out.append((rid, page, str(alias) if alias else None))
    except sqlite3.Error as exc:
        log.warning("PagerMon DB read failed: %s", exc)
    finally:
        conn.close()
    return out, high
