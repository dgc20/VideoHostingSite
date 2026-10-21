"""Stage 1 of the pipeline: getting footage in.

Every entry point — the upload form, resumable uploads (ING-1), bulk import
(ING-2) and the iCloud pipeline's /api/import — validates metadata the same
way (ING-4) and calls create_video(), which records the video and queues its
processing jobs. The jobs themselves (hashing, storing the original,
transcoding, keyframes) run in the background; see jobs.py and stages.py.
"""
import json
import mimetypes
import os
import re
import uuid
from datetime import date

import db
import jobs

ALLOWED_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v"}
# Bulk/folder import also accepts common camera/archive formats; they're
# transcoded to an MP4 mezzanine before anyone plays them.
IMPORT_EXTENSIONS = ALLOWED_EXTENSIONS | {".mkv", ".avi", ".mts", ".m2ts", ".ts", ".wmv"}

# Suggestions shown in the upload form; any non-empty value is accepted.
LICENSE_SUGGESTIONS = (
    "own-footage",
    "competitor-consent",
    "event-licensed",
    "broadcast-internal-analysis-only",
    "creative-commons",
)

MAX_PARTICIPANTS = 20
_PARTICIPANT_RE = re.compile(r"^(?P<name>[^()]+?)\s*(?:\((?P<team>[^()]*)\))?$")


class MetadataError(ValueError):
    pass


def _text(data, key, limit, required=False, label=None):
    value = data.get(key)
    value = "" if value is None else str(value).strip()
    label = label or key.replace("_", " ")
    if required and not value:
        raise MetadataError(f"{label} is required")
    if len(value) > limit:
        raise MetadataError(f"{label} is too long ({limit} characters max)")
    return value


def parse_participants(value):
    """Parse participants into [{"name", "team"}].

    Accepts a list of strings/dicts (JSON API) or text with one competitor
    per line or separated by ';', each as "Name" or "Name (Team)".
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = [p for p in re.split(r"[;\n]", value) if p.strip()]
    elif isinstance(value, list):
        items = value
    else:
        raise MetadataError("participants must be a list or text")
    if len(items) > MAX_PARTICIPANTS:
        raise MetadataError(f"at most {MAX_PARTICIPANTS} participants")
    result = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            team = str(item.get("team") or "").strip()
        else:
            match = _PARTICIPANT_RE.match(str(item).strip())
            if not match:
                raise MetadataError(f"can't read participant {item!r}; use 'Name (Team)'")
            name, team = match.group("name").strip(), (match.group("team") or "").strip()
        if not name:
            raise MetadataError("participant name can't be empty")
        if len(name) > 100 or len(team) > 100:
            raise MetadataError("participant names and teams are 100 characters max")
        result.append({"name": name, "team": team})
    return result


def validate_metadata(data, filename=None):
    """ING-4: validate the metadata attached to new footage.

    Returns a normalised dict; raises MetadataError with a user-facing
    message. A license is mandatory: the spec requires ingestion to refuse
    files without one.
    """
    meta = {
        "title": _text(data, "title", 200),
        "description": _text(data, "description", 5000),
        "competition": _text(data, "competition", 200),
        "source": _text(data, "source", 500),
        "license": _text(data, "license", 200, required=True),
        "event_date": None,
        "participants": parse_participants(data.get("participants")),
    }
    raw_date = _text(data, "event_date", 10, label="date")
    if raw_date:
        try:
            meta["event_date"] = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            raise MetadataError("date must be YYYY-MM-DD") from None
    if not meta["title"]:
        base = os.path.splitext(os.path.basename(filename or ""))[0]
        meta["title"] = base[:200] or "Untitled"
    return meta


def create_video(conn, *, user_id, meta, filename, size_bytes, ingest_input,
                 source_id=None, max_attempts=3):
    """Insert a video in 'processing' state and queue its pipeline.

    The caller commits and then calls jobs.notify().
    """
    video_id = uuid.uuid4().hex
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    conn.execute(
        "INSERT INTO videos (id, title, description, stored_name, content_type,"
        " size_bytes, user_id, status, source_id, competition, event_date,"
        " source, license, uploaded_at)"
        " VALUES (?, ?, ?, '', ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?)",
        (video_id, meta["title"], meta["description"], content_type, size_bytes,
         user_id, source_id, meta["competition"], meta["event_date"],
         meta["source"], meta["license"], db.utcnow()),
    )
    for position, person in enumerate(meta["participants"]):
        conn.execute(
            "INSERT INTO video_participants (video_id, position, name, team)"
            " VALUES (?, ?, ?, ?)",
            (video_id, position, person["name"], person["team"]),
        )
    jobs.enqueue_pipeline(conn, video_id, ingest_input, max_attempts)
    return video_id


def delete_video_rows(conn, video_id):
    """Remove a video and everything that hangs off it. Caller commits."""
    for table in ("comments", "video_participants", "assets", "processing_jobs"):
        conn.execute(f"DELETE FROM {table} WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))


def participants_for(conn, video_id):
    return conn.execute(
        "SELECT name, team FROM video_participants WHERE video_id = ?"
        " ORDER BY position",
        (video_id,),
    ).fetchall()


def metadata_json(meta):
    return json.dumps(meta)
