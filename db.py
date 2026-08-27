"""SQLite persistence for dialoguearr.

The worker previously kept no state at all, so every run's measurements and
outcomes were lost with the container's logs. `files` holds current truth and
can be seeded from Plex, `runs` is the append-only history.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

DB_PATH = os.environ.get("DB_PATH", "/state/dialoguearr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    library          TEXT,
    name             TEXT,
    duration         REAL,
    size             INTEGER,
    state            TEXT NOT NULL,
    skip_reason      TEXT,
    source_lang      TEXT,
    source_codec     TEXT,
    source_channels  INTEGER,
    input_i          REAL,
    input_lra        REAL,
    input_tp         REAL,
    output_i         REAL,
    measurement      TEXT,
    mode             TEXT,
    processed_at     TEXT,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    name         TEXT,
    trigger      TEXT,
    outcome      TEXT NOT NULL,
    detail       TEXT,
    mode         TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    seconds      REAL
);

CREATE INDEX IF NOT EXISTS idx_files_state ON files(state);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""

_write_lock = threading.Lock()


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init():
    with connect() as conn:
        # WAL lets the web thread read while the worker is mid-write.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def upsert_file(path, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{c}=excluded.{c}" for c in fields)
    with _write_lock, connect() as conn:
        conn.execute(
            f"INSERT INTO files (path, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}",
            (str(path), *fields.values()),
        )
        conn.commit()


def start_run(path, name, trigger):
    with _write_lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (path, name, trigger, outcome, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (str(path), name, trigger, now()),
        )
        conn.commit()
        return cur.lastrowid


def finish_run(run_id, outcome, detail=None, mode=None):
    with _write_lock, connect() as conn:
        row = conn.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
        seconds = None
        if row:
            started = datetime.fromisoformat(row["started_at"])
            seconds = (datetime.now(UTC) - started).total_seconds()
        conn.execute(
            "UPDATE runs SET outcome=?, detail=?, mode=?, finished_at=?, seconds=? WHERE id=?",
            (outcome, detail, mode, now(), seconds, run_id),
        )
        conn.commit()


def summary():
    with connect() as conn:
        by_state = {r["state"]: r["n"] for r in
                    conn.execute("SELECT state, COUNT(*) n FROM files GROUP BY state")}
        by_lang = {r["source_lang"] or "untagged": r["n"] for r in conn.execute(
            "SELECT source_lang, COUNT(*) n FROM files WHERE state='done' "
            "GROUP BY source_lang ORDER BY n DESC")}
        recent = conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE outcome='replaced' "
            "AND started_at > datetime('now', '-7 days')").fetchone()["n"]
        failures = conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE outcome='failed'").fetchone()["n"]
        gain = conn.execute(
            "SELECT AVG(output_i - input_i) g FROM files "
            "WHERE input_i IS NOT NULL AND output_i IS NOT NULL").fetchone()["g"]
        last = conn.execute(
            "SELECT name, outcome, started_at FROM runs "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
    return {
        "done": by_state.get("done", 0),
        "eligible": by_state.get("eligible", 0),
        "skipped": by_state.get("skipped", 0),
        "failed": by_state.get("failed", 0),
        "by_language": by_lang,
        "replaced_7d": recent,
        "failures_total": failures,
        "avg_gain_db": round(gain, 1) if gain is not None else None,
        "last_run": dict(last) if last else None,
    }


def list_files(state=None, query=None, limit=500):
    sql = "SELECT * FROM files"
    where, args = [], []
    if state:
        where.append("state = ?")
        args.append(state)
    if query:
        where.append("name LIKE ?")
        args.append(f"%{query}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(processed_at, updated_at) DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def list_runs(limit=100):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]
