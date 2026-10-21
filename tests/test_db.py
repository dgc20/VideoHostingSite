"""STO-2: metadata in a relational DB, migrated in place on old databases."""
import sqlite3

import db

OLD_SCHEMA = """
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE videos (id TEXT PRIMARY KEY, title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    views INTEGER NOT NULL DEFAULT 0);
INSERT INTO videos (id, title, stored_name, content_type, size_bytes)
    VALUES ('old1', 'Old video', 'old1.mp4', 'video/mp4', 10);
"""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_old_database_is_migrated(tmp_path):
    from flask import Flask

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()

    app = Flask(__name__)
    app.config["DATABASE"] = str(path)
    db.init_db(app)

    conn = sqlite3.connect(path)
    cols = _columns(conn, "videos")
    for name, _ in db.VIDEO_PIPELINE_COLUMNS:
        assert name in cols
    for table in ("assets", "processing_jobs", "video_participants", "upload_sessions"):
        assert _columns(conn, table), table
    row = conn.execute("SELECT status, license FROM videos WHERE id='old1'").fetchone()
    assert row == ("ready", "")
    conn.close()


def test_fresh_database_has_pipeline_schema(appmod):
    conn = db.connect(appmod.app.config["DATABASE"])
    assert {"content_hash", "license", "duration_ms"} <= _columns(conn, "videos")
    assert {"kind", "uri", "ts_ms", "pipeline_version"} <= _columns(conn, "assets")
    assert {"stage", "status", "attempts", "error", "model_version"} <= _columns(conn, "processing_jobs")
    conn.close()
