"""SQLite helpers for users and video metadata."""
import sqlite3
from datetime import datetime, timezone

from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    belt          TEXT NOT NULL DEFAULT 'white'
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
    source_id   TEXT,
    -- Stored name of the poster image extracted from the video. NULL for
    -- older videos and any whose frame couldn't be extracted.
    thumbnail_name TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id         TEXT PRIMARY KEY,
    video_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);

-- Phase 0 pipeline tables. Written to port to Postgres type-for-type:
-- app-generated TEXT ids, app-written UTC timestamps, JSON kept as TEXT
-- (JSONB later), and declared foreign keys.

-- Competitors in a match (the spec's Video.participants[]): for BJJ, an
-- athlete and their academy/team.
CREATE TABLE IF NOT EXISTS video_participants (
    video_id TEXT NOT NULL REFERENCES videos(id),
    position INTEGER NOT NULL,
    name     TEXT NOT NULL,
    team     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (video_id, position)
);

-- Every file in object storage that belongs to a video: the immutable
-- original and its derivatives (STO-1). uri is the storage key.
CREATE TABLE IF NOT EXISTS assets (
    id               TEXT PRIMARY KEY,
    video_id         TEXT NOT NULL REFERENCES videos(id),
    kind             TEXT NOT NULL,  -- original, mezzanine, proxy, keyframe, thumbnail
    uri              TEXT NOT NULL,
    codec            TEXT,
    size_bytes       INTEGER,
    width            INTEGER,
    height           INTEGER,
    ts_ms            INTEGER,        -- keyframes: position in the video
    pipeline_version TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (video_id, uri)
);

-- One row per pipeline stage per video (the spec's ProcessingJob).
CREATE TABLE IF NOT EXISTS processing_jobs (
    id            TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES videos(id),
    stage         TEXT NOT NULL,     -- ingest, probe, transcode, keyframes
    status        TEXT NOT NULL,     -- queued, running, succeeded, failed, skipped
    model_version TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    error         TEXT,
    input         TEXT,              -- JSON
    locked_until  TEXT,              -- lease held by the worker running it
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    UNIQUE (video_id, stage)
);

-- Resumable uploads in progress (ING-1).
CREATE TABLE IF NOT EXISTS upload_sessions (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id),
    filename       TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    received_bytes INTEGER NOT NULL DEFAULT 0,
    metadata       TEXT NOT NULL,    -- JSON: title, license, participants...
    status         TEXT NOT NULL,    -- open, complete, aborted
    video_id       TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_video ON assets(video_id, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON processing_jobs(status);
"""

# Columns added to videos for the analysis pipeline (ING-4, STO-2), in the
# order they're migrated onto older databases.
VIDEO_PIPELINE_COLUMNS = (
    ("competition", "TEXT NOT NULL DEFAULT ''"),
    ("event_date", "TEXT"),        # ISO date (YYYY-MM-DD) of the match
    ("source", "TEXT NOT NULL DEFAULT ''"),
    ("license", "TEXT NOT NULL DEFAULT ''"),
    ("duration_ms", "INTEGER"),
    ("fps", "REAL"),
    ("width", "INTEGER"),
    ("height", "INTEGER"),
    ("content_hash", "TEXT"),      # sha256 of the original
    ("duplicate_of", "TEXT"),      # set when status = 'duplicate'
)


def utcnow():
    """Current UTC time in the same format SQLite's datetime('now') uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(database):
    """Open a connection outside a request (background workers, CLI)."""
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    if "db" not in g:
        # Generous busy timeout: the background job worker writes too.
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(db):
    """Bring an existing database up to the current schema."""
    _migrate_users(db)
    _migrate_videos(db)


def _migrate_users(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "belt" not in columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN belt TEXT NOT NULL DEFAULT 'white'"
        )


def _migrate_videos(db):
    """Bring the videos table up to the current schema.

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
    if "thumbnail_name" not in columns:
        db.execute("ALTER TABLE videos ADD COLUMN thumbnail_name TEXT")
    for name, decl in VIDEO_PIPELINE_COLUMNS:
        if name not in columns:
            db.execute(f"ALTER TABLE videos ADD COLUMN {name} {decl}")

    # Created unconditionally (IF NOT EXISTS) so both fresh and migrated
    # databases end up with the index, now that source_id is guaranteed.
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_source"
        " ON videos(source_id) WHERE source_id IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_hash ON videos(content_hash)"
    )


def init_db(app):
    with app.app_context():
        db = sqlite3.connect(app.config["DATABASE"])
        db.executescript(SCHEMA)
        _migrate(db)
        db.commit()
        db.close()
    app.teardown_appcontext(close_db)
