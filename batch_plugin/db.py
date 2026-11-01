"""SQLite storage for the batch video plugin.

Standard library only. One connection guarded by a re-entrant lock so the
HTTP threads and the background generation worker can share it safely.
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    idea TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '记录',
    orientation TEXT NOT NULL DEFAULT 'landscape',
    takes_per_shot INTEGER NOT NULL DEFAULT 3,
    auto_mode INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'planning',
    post_status TEXT NOT NULL DEFAULT 'none',
    publish_copy TEXT NOT NULL DEFAULT '',
    character_lock TEXT NOT NULL DEFAULT '',
    location_lock TEXT NOT NULL DEFAULT '',
    visual_lock TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    camera TEXT NOT NULL DEFAULT '',
    visual TEXT NOT NULL DEFAULT '',
    dialogue TEXT NOT NULL DEFAULT '',
    start_state TEXT NOT NULL DEFAULT '',
    end_state TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    prompt_full TEXT NOT NULL DEFAULT '',
    selected_take_id INTEGER,
    UNIQUE(project_id, seq)
);
CREATE TABLE IF NOT EXISTS takes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    take_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    file_path TEXT NOT NULL DEFAULT '',
    check_note TEXT NOT NULL DEFAULT '',
    score_continuity INTEGER,
    score_realism INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(shot_id, take_no)
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'generate',
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT NOT NULL DEFAULT '',
    successes INTEGER NOT NULL DEFAULT 0,
    last_activity INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    take_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    realism_score INTEGER,
    continuity_score INTEGER,
    fake_signals TEXT NOT NULL DEFAULT '[]',
    issues TEXT NOT NULL DEFAULT '',
    color_note TEXT NOT NULL DEFAULT '',
    seam_note TEXT NOT NULL DEFAULT '',
    edit_suggestion TEXT NOT NULL DEFAULT '',
    trim_head_sec REAL NOT NULL DEFAULT 0,
    trim_tail_sec REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'auto',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS final_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    plan_id INTEGER,
    file_path TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def row_to_dict(self, row):
        return dict(row) if row is not None else None
