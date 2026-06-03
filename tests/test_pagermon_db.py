"""Tests for the PagerMon SQLite ingest adapter across schema variations.

The whole value of `app.pagermon_db` is that it adapts to the differing shapes a
real PagerMon messages.db can take. Each test below builds a throwaway DB in a
different shape and asserts the probe maps it correctly and reads rows back.

Run:  python tests/test_pagermon_db.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import pagermon_db as pm


def _db(rows_sql: str, inserts: list[tuple] = (), insert_sql: str = "") -> str:
    path = str(Path(tempfile.mkdtemp(prefix="pmdb_")) / "messages.db")
    conn = sqlite3.connect(path)
    conn.executescript(rows_sql)
    for row in inserts:
        conn.execute(insert_sql, row)
    conn.commit()
    conn.close()
    return path


def test_standard_schema_with_capcodes_join():
    """Canonical PagerMon: address/message/source/timestamp(ms) + alias via FK."""
    path = _db(
        """
        CREATE TABLE capcodes (id INTEGER PRIMARY KEY, address TEXT, alias TEXT, agency TEXT, ignore INT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, address TEXT, message TEXT,
                               source TEXT, timestamp INTEGER, alias_id INTEGER);
        INSERT INTO capcodes (id, address, alias) VALUES (7, '1234567', 'Station 1 Dispatch');
        """,
        inserts=[
            ("1234567", "STRUCTURE FIRE 12 MAIN ST", "sdr1", 1735000000000, 7),
        ],
        insert_sql="INSERT INTO messages (address, message, source, timestamp, alias_id) VALUES (?,?,?,?,?)",
    )
    m = pm.probe_schema(path)
    assert m.detected and m.table == "messages"
    assert m.capcode_col == "address" and m.message_col == "message"
    assert m.timestamp_kind == "millis"
    assert m.alias_fk_col == "alias_id" and m.capcodes_table == "capcodes"
    rows, high = pm.fetch_new(path, m, 0)
    assert len(rows) == 1 and high == 1
    rid, page, alias = rows[0]
    assert page.capcode == "1234567"
    assert page.message == "STRUCTURE FIRE 12 MAIN ST"
    assert alias == "Station 1 Dispatch"
    assert page.received_at.year == 2024  # 1.735e12 ms ~ Dec 2024
    print("ok: standard schema + capcodes join")


def test_seconds_timestamp_and_inline_alias():
    """Older fork: epoch seconds, label held inline on messages.alias, no FK."""
    path = _db(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, address TEXT, message TEXT,
                               timestamp INTEGER, alias TEXT);
        """,
        inserts=[("999", "TEST PAGE", 1735000000, "Admin")],
        insert_sql="INSERT INTO messages (address, message, timestamp, alias) VALUES (?,?,?,?)",
    )
    m = pm.probe_schema(path)
    assert m.detected
    assert m.timestamp_kind == "seconds"
    assert m.alias_text_col == "alias" and m.alias_fk_col is None
    rows, _ = pm.fetch_new(path, m, 0)
    _, page, alias = rows[0]
    assert alias == "Admin" and page.received_at.year == 2024
    print("ok: seconds timestamp + inline alias")


def test_renamed_columns_fork():
    """A fork that renamed columns: capcode/text/time, no alias at all."""
    path = _db(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, capcode TEXT, text TEXT, time INTEGER);
        """,
        inserts=[("555", "GRASS FIRE", 1735000000)],
        insert_sql="INSERT INTO messages (capcode, text, time) VALUES (?,?,?)",
    )
    m = pm.probe_schema(path)
    assert m.capcode_col == "capcode" and m.message_col == "text"
    assert m.timestamp_col == "time"
    rows, _ = pm.fetch_new(path, m, 0)
    _, page, alias = rows[0]
    assert page.capcode == "555" and page.message == "GRASS FIRE" and alias is None
    print("ok: renamed-columns fork")


def test_iso_timestamp_string():
    """Timestamp stored as an ISO text string rather than an epoch number."""
    path = _db(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, address TEXT, message TEXT, datetime TEXT);
        """,
        inserts=[("42", "MVA HWY 1", "2025-03-04T09:30:00+00:00")],
        insert_sql="INSERT INTO messages (address, message, datetime) VALUES (?,?,?)",
    )
    m = pm.probe_schema(path)
    assert m.timestamp_kind == "iso" and m.timestamp_col == "datetime"
    rows, _ = pm.fetch_new(path, m, 0)
    _, page, _alias = rows[0]
    assert page.received_at.year == 2025 and page.received_at.month == 3
    print("ok: iso timestamp string")


def test_incremental_and_high_water():
    """fetch_new only returns rows past last_id and advances the high-water mark."""
    path = _db(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, address TEXT, message TEXT, timestamp INTEGER);
        """,
        inserts=[("1", "A", 1735000000), ("1", "B", 1735000001), ("1", "C", 1735000002)],
        insert_sql="INSERT INTO messages (address, message, timestamp) VALUES (?,?,?)",
    )
    m = pm.probe_schema(path)
    rows, high = pm.fetch_new(path, m, 1)         # skip id=1
    assert [p.message for _i, p, _a in rows] == ["B", "C"] and high == 3
    rows2, high2 = pm.fetch_new(path, m, high)    # nothing new
    assert rows2 == [] and high2 == 3
    print("ok: incremental fetch + high-water")


def test_missing_file_and_override():
    """Missing file yields a non-detected mapping with a note (no crash);
    a manual override forces a mapping the probe couldn't guess."""
    m = pm.probe_schema("/no/such/file.db")
    assert not m.detected and "not found" in m.note.lower()

    # Ambiguous table the heuristics wouldn't pick — override names it.
    path = _db(
        """
        CREATE TABLE weird (id INTEGER PRIMARY KEY, ric TEXT, alpha TEXT, epoch INTEGER);
        """,
        inserts=[("88", "HAZMAT", 1735000000)],
        insert_sql="INSERT INTO weird (ric, alpha, epoch) VALUES (?,?,?)",
    )
    auto = pm.probe_schema(path)
    forced = pm.probe_schema(path, {"table": "weird", "capcode_col": "ric",
                                    "message_col": "alpha", "timestamp_col": "epoch"})
    assert forced.capcode_col == "ric" and forced.message_col == "alpha"
    rows, _ = pm.fetch_new(path, forced, 0)
    assert rows and rows[0][1].message == "HAZMAT"
    print("ok: missing file + manual override (auto note: %s)" % auto.note[:40])


if __name__ == "__main__":
    test_standard_schema_with_capcodes_join()
    test_seconds_timestamp_and_inline_alias()
    test_renamed_columns_fork()
    test_iso_timestamp_string()
    test_incremental_and_high_water()
    test_missing_file_and_override()
    print("\nAll PagerMon DB adapter tests passed.")
