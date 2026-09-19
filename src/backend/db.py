"""SQLite storage for DAP4Y.

Everything Gemini extracts lands here as structured rows. The frontend never
touches this module directly -- it goes through ``backend.app``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("DAP4Y_DB", REPO_ROOT / "data" / "dap4y.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS student (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    name                  TEXT    NOT NULL DEFAULT 'Student',
    term                  TEXT    NOT NULL DEFAULT '',
    study_habits          TEXT    NOT NULL DEFAULT '',
    preferred_windows     TEXT    NOT NULL DEFAULT '[]',   -- JSON list of {day,start,end}
    session_minutes       INTEGER NOT NULL DEFAULT 90,
    break_minutes         INTEGER NOT NULL DEFAULT 15,
    daily_capacity_min    INTEGER NOT NULL DEFAULT 180
);

CREATE TABLE IF NOT EXISTS course (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL,
    name              TEXT    NOT NULL DEFAULT '',
    instructor        TEXT    NOT NULL DEFAULT '',
    textbook          TEXT    NOT NULL DEFAULT '',
    credits           REAL    NOT NULL DEFAULT 3,
    difficulty        INTEGER NOT NULL DEFAULT 3,        -- 1..5, effective value
    difficulty_source TEXT    NOT NULL DEFAULT 'gemini', -- gemini|uoftindex|manual
    colour            TEXT    NOT NULL DEFAULT '#6c8ebf',
    -- UofT Index evidence behind the difficulty call
    uoft_code         TEXT    NOT NULL DEFAULT '',       -- resolved e.g. CSC373H1
    uoft_drop_rate    REAL,
    uoft_workload     REAL,
    uoft_rating       REAL,
    uoft_reviews      INTEGER,
    uoft_confidence   TEXT    NOT NULL DEFAULT '',
    uoft_reasoning    TEXT    NOT NULL DEFAULT '',
    weekly_study_min  INTEGER NOT NULL DEFAULT 0,        -- Gemini's per-week estimate
    UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS assessment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'assignment',  -- assignment|quiz|midterm|final|project|lab
    due_date    TEXT,                                   -- ISO yyyy-mm-dd
    weight_pct  REAL    NOT NULL DEFAULT 0,
    notes       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open'         -- open|done
);

CREATE TABLE IF NOT EXISTS topic (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'lecture',      -- lecture|tutorial|lab|reading
    week       INTEGER,
    summary    TEXT    NOT NULL DEFAULT '',
    keywords   TEXT    NOT NULL DEFAULT '[]'            -- JSON list
);

CREATE TABLE IF NOT EXISTS todo (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id      INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    topic_id       INTEGER REFERENCES topic(id) ON DELETE SET NULL,
    assessment_id  INTEGER REFERENCES assessment(id) ON DELETE SET NULL,
    title          TEXT    NOT NULL,
    detail         TEXT    NOT NULL DEFAULT '',
    est_minutes    INTEGER NOT NULL DEFAULT 45,
    due_date       TEXT,
    status         TEXT    NOT NULL DEFAULT 'open',     -- open|done
    priority       REAL    NOT NULL DEFAULT 0,
    priority_why   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS session (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,                       -- ISO yyyy-mm-dd
    start_time  TEXT    NOT NULL,                       -- HH:MM
    end_time    TEXT    NOT NULL,
    course_id   INTEGER REFERENCES course(id) ON DELETE CASCADE,
    focus       TEXT    NOT NULL DEFAULT '',
    rationale   TEXT    NOT NULL DEFAULT '',
    todo_ids    TEXT    NOT NULL DEFAULT '[]',          -- JSON list of todo ids
    status      TEXT    NOT NULL DEFAULT 'planned',     -- planned|done|skipped
    -- 'preplan' blocks are created empty by the planning stage; a later stage
    -- fills todo_ids. 'filled' marks one that has been populated.
    stage       TEXT    NOT NULL DEFAULT 'preplan'      -- preplan|filled
);

CREATE TABLE IF NOT EXISTS question (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    topic_id   INTEGER REFERENCES topic(id) ON DELETE SET NULL,
    prompt     TEXT    NOT NULL,
    answer     TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'short',        -- short|mcq|numeric|proof
    options    TEXT    NOT NULL DEFAULT '[]',           -- JSON list for mcq
    difficulty INTEGER NOT NULL DEFAULT 3,
    origin     TEXT    NOT NULL DEFAULT 'generated'     -- generated|past_paper
);

CREATE TABLE IF NOT EXISTS attempt (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES question(id) ON DELETE CASCADE,
    answer      TEXT    NOT NULL DEFAULT '',
    score       REAL    NOT NULL DEFAULT 0,             -- 0..1
    feedback    TEXT    NOT NULL DEFAULT '',
    gaps        TEXT    NOT NULL DEFAULT '[]',          -- JSON list of misconception tags
    attempt_no  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mastery (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    topic_id   INTEGER REFERENCES topic(id) ON DELETE CASCADE,
    label      TEXT    NOT NULL,
    score      REAL    NOT NULL DEFAULT 0.5,            -- 0 struggling .. 1 solid
    samples    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (course_id, label)
);

CREATE TABLE IF NOT EXISTS resource (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    label     TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL,
    kind      TEXT    NOT NULL DEFAULT 'reading',       -- reading|video|practice|tool
    locator   TEXT    NOT NULL DEFAULT '',              -- chapter ref, search query, or URL
    why       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS overlap (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT    NOT NULL,
    course_ids TEXT    NOT NULL DEFAULT '[]',           -- JSON list
    payoff     TEXT    NOT NULL DEFAULT '',
    saved_min  INTEGER NOT NULL DEFAULT 0
);

-- When the student is free to study, declared by hand: one window per weekday.
-- This is the only time constraint the planner gets.
CREATE TABLE IF NOT EXISTS availability (
    weekday    INTEGER PRIMARY KEY,              -- 0 = Monday .. 6 = Sunday
    available  INTEGER NOT NULL DEFAULT 1,
    start_time TEXT    NOT NULL DEFAULT '17:00',
    end_time   TEXT    NOT NULL DEFAULT '22:00'
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,                        -- syllabus|notes|past_test|intent
    filename   TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL DEFAULT '',
    payload    TEXT    NOT NULL DEFAULT '{}',           -- raw Gemini JSON, for the demo "show your work"
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied by hand.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("course", "uoft_code", "TEXT NOT NULL DEFAULT ''"),
    ("course", "uoft_drop_rate", "REAL"),
    ("course", "uoft_workload", "REAL"),
    ("course", "uoft_rating", "REAL"),
    ("course", "uoft_reviews", "INTEGER"),
    ("course", "uoft_confidence", "TEXT NOT NULL DEFAULT ''"),
    ("course", "uoft_reasoning", "TEXT NOT NULL DEFAULT ''"),
    ("course", "weekly_study_min", "INTEGER NOT NULL DEFAULT 0"),
    ("session", "stage", "TEXT NOT NULL DEFAULT 'preplan'"),
]

DEFAULT_AVAILABILITY = [
    (0, 1, "17:00", "22:00"), (1, 1, "17:00", "22:00"), (2, 1, "17:00", "22:00"),
    (3, 1, "17:00", "22:00"), (4, 1, "17:00", "20:00"), (5, 1, "10:00", "16:00"),
    (6, 1, "12:00", "20:00"),
]


def _migrate(conn) -> None:
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO student (id, name) VALUES (1, 'Student')"
        )
        conn.executemany(
            "INSERT OR IGNORE INTO availability "
            "(weekday, available, start_time, end_time) VALUES (?, ?, ?, ?)",
            DEFAULT_AVAILABILITY,
        )


def reset_db() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------

def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid


def insert(table: str, data: dict) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))


def update(table: str, row_id: int, data: dict) -> None:
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*data.values(), row_id])


def delete(table: str, row_id: int) -> None:
    execute(f"DELETE FROM {table} WHERE id = ?", [row_id])


def jload(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw) if raw else (default if default is not None else [])
    except (TypeError, ValueError):
        return default if default is not None else []


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
