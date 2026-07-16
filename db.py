"""SQLite helpers for users and video metadata."""
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    views       INTEGER NOT NULL DEFAULT 0,
    user_id     TEXT,
    status      TEXT NOT NULL DEFAULT 'ready',
    -- Stable id from an external source (e.g. an iCloud asset), so the
    -- import pipeline can re-run without creating duplicates. NULL for
    -- videos uploaded through the website.
    source_id   TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id         TEXT PRIMARY KEY,
    video_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(db):
    """Bring an existing database up to the current schema.

    Columns are added first, then indexes are (re)created — never the other
    way round. An index in SCHEMA would run inside executescript() *before*
    these ALTERs, and on a pre-existing table (where CREATE TABLE IF NOT
    EXISTS is a no-op) it would reference a column that doesn't exist yet.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
    if "user_id" not in columns:
        # Videos uploaded before accounts existed keep user_id = NULL.
        db.execute("ALTER TABLE videos ADD COLUMN user_id TEXT")
    if "status" not in columns:
        # Videos uploaded before compression existed are all ready.
        db.execute(
            "ALTER TABLE videos ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'"
        )
    if "source_id" not in columns:
        db.execute("ALTER TABLE videos ADD COLUMN source_id TEXT")

    # Created unconditionally (IF NOT EXISTS) so both fresh and migrated
    # databases end up with the index, now that source_id is guaranteed.
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_source"
        " ON videos(source_id) WHERE source_id IS NOT NULL"
    )


def init_db(app):
    with app.app_context():
        db = sqlite3.connect(app.config["DATABASE"])
        db.executescript(SCHEMA)
        _migrate(db)
        db.commit()
        db.close()
    app.teardown_appcontext(close_db)
