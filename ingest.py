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


# --- resumable uploads (ING-1) ---------------------------------------------
#
# A small tus-like protocol, session-authenticated like the rest of the site:
#
#   POST   /uploads        {filename, size, ...metadata} -> {upload_id, offset}
#   PUT    /uploads/<id>   body = bytes, Content-Range: bytes <start>-<end>/<size>
#   GET    /uploads/<id>   -> {offset, size, status, video_id}
#   DELETE /uploads/<id>   abandon the upload
#
# Chunks are appended to a staging file on local disk; the server's offset is
# the source of truth, so a client that lost its connection asks for the
# offset and continues from there. When the last byte arrives the upload
# becomes a video and its processing jobs are queued.

_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class UploadError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def staging_path(incoming_dir, upload_id):
    return os.path.join(incoming_dir, f"resumable_{upload_id}.part")


def create_upload_session(conn, *, user_id, data, max_bytes, incoming_dir,
                          free_bytes=None):
    filename = os.path.basename(str(data.get("filename") or "").strip())
    if not filename:
        raise UploadError("filename is required")
    if os.path.splitext(filename)[1].lower() not in ALLOWED_EXTENSIONS:
        raise UploadError(
            "unsupported file type; allowed: " + ", ".join(sorted(ALLOWED_EXTENSIONS))
        )
    try:
        size = int(data.get("size"))
    except (TypeError, ValueError):
        raise UploadError("size must be the file size in bytes") from None
    if size <= 0:
        raise UploadError("size must be the file size in bytes")
    if size > max_bytes:
        raise UploadError(
            f"file is too large ({max_bytes // (1024 ** 3)} GB max)", status=413
        )
    if free_bytes is not None and size > free_bytes:
        raise UploadError("not enough disk space on the server for this upload",
                          status=507)
    try:
        meta = validate_metadata(data, filename)
    except MetadataError as exc:
        raise UploadError(str(exc)) from None

    upload_id = uuid.uuid4().hex
    now = db.utcnow()
    conn.execute(
        "INSERT INTO upload_sessions (id, user_id, filename, size_bytes,"
        " received_bytes, metadata, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 0, ?, 'open', ?, ?)",
        (upload_id, user_id, filename, size, metadata_json(meta), now, now),
    )
    # Create the staging file now so every chunk is a positioned write.
    open(staging_path(incoming_dir, upload_id), "wb").close()
    return upload_id


def get_upload_session(conn, upload_id, user_id):
    row = conn.execute(
        "SELECT * FROM upload_sessions WHERE id = ? AND user_id = ?",
        (upload_id, user_id),
    ).fetchone()
    if row is None:
        raise UploadError("upload not found", status=404)
    return row


def write_chunk(conn, session, content_range, stream, *, max_chunk,
                incoming_dir, max_attempts=3):
    """Write one chunk at its offset. Returns the updated session row.

    The chunk must start exactly at the server's current offset (else 409
    with the offset to resume from). The offset only advances once the
    whole chunk is on disk, so an interrupted request can simply be resent.
    """
    if session["status"] != "open":
        raise UploadError(f"upload is {session['status']}", status=409,
                          offset=session["received_bytes"])
    match = _CONTENT_RANGE_RE.match(content_range or "")
    if not match:
        raise UploadError("Content-Range must be 'bytes <start>-<end>/<size>'")
    start, end, total = (int(x) for x in match.groups())
    if total != session["size_bytes"] or end < start or end >= total:
        raise UploadError("Content-Range doesn't match this upload")
    length = end - start + 1
    if length > max_chunk:
        raise UploadError(f"chunk too large ({max_chunk} bytes max)", status=413)
    if start != session["received_bytes"]:
        raise UploadError("chunk doesn't start at the current offset", status=409,
                          offset=session["received_bytes"])

    path = staging_path(incoming_dir, session["id"])
    if not os.path.exists(path):
        raise UploadError("upload data is missing; start a new upload", status=410)
    written = 0
    with open(path, "r+b") as f:
        f.seek(start)
        while written < length:
            data = stream.read(min(1024 * 1024, length - written))
            if not data:
                break
            f.write(data)
            written += len(data)
        if stream.read(1):
            raise UploadError("body is longer than Content-Range says")
    if written != length:
        raise UploadError("body is shorter than Content-Range says", status=400,
                          offset=session["received_bytes"])

    cur = conn.execute(
        "UPDATE upload_sessions SET received_bytes = ?, updated_at = ?"
        " WHERE id = ? AND received_bytes = ? AND status = 'open'",
        (end + 1, db.utcnow(), session["id"], start),
    )
    if cur.rowcount != 1:
        conn.rollback()
        fresh = conn.execute(
            "SELECT received_bytes FROM upload_sessions WHERE id = ?", (session["id"],)
        ).fetchone()
        raise UploadError("concurrent write to this upload", status=409,
                          offset=fresh["received_bytes"])
    if end + 1 == session["size_bytes"]:
        _finish_upload(conn, session, path, max_attempts)
    conn.commit()
    return conn.execute(
        "SELECT * FROM upload_sessions WHERE id = ?", (session["id"],)
    ).fetchone()


def _finish_upload(conn, session, path, max_attempts):
    meta = json.loads(session["metadata"])
    ext = os.path.splitext(session["filename"])[1].lower()
    staged = os.path.join(os.path.dirname(path), f"upload_{session['id']}{ext}")
    os.replace(path, staged)
    video_id = create_video(
        conn,
        user_id=session["user_id"],
        meta=meta,
        filename=session["filename"],
        size_bytes=session["size_bytes"],
        ingest_input={"staged_path": staged, "delete_staged": True,
                      "filename": session["filename"]},
        max_attempts=max_attempts,
    )
    conn.execute(
        "UPDATE upload_sessions SET status = 'complete', video_id = ?,"
        " updated_at = ? WHERE id = ?",
        (video_id, db.utcnow(), session["id"]),
    )


def abort_upload(conn, session, incoming_dir):
    if session["status"] == "open":
        conn.execute(
            "UPDATE upload_sessions SET status = 'aborted', updated_at = ? WHERE id = ?",
            (db.utcnow(), session["id"]),
        )
        try:
            os.remove(staging_path(incoming_dir, session["id"]))
        except FileNotFoundError:
            pass


def expire_upload_sessions(conn, incoming_dir, max_age_hours):
    """Abort open uploads untouched for max_age_hours and free their disk."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%d %H:%M:%S")
    stale = conn.execute(
        "SELECT * FROM upload_sessions WHERE status = 'open' AND updated_at < ?",
        (cutoff,),
    ).fetchall()
    for session in stale:
        abort_upload(conn, session, incoming_dir)
    return len(stale)


def upload_status(session):
    return {
        "upload_id": session["id"],
        "offset": session["received_bytes"],
        "size": session["size_bytes"],
        "status": session["status"],
        "video_id": session["video_id"],
    }


# --- bulk import (ING-2) -----------------------------------------------------
#
# Import every video under a local folder, or under a key prefix in the app's
# own storage (the local uploads dir or the Azure container — the repo's
# equivalent of "an S3/GCS bucket"). Metadata comes from defaults given on
# the command line, overridden per file by an optional CSV manifest with a
# `file` column (path relative to the folder/prefix, or bare file name) and
# any of: title, description, competition, event_date, participants, source,
# license. Re-running is idempotent: each file has a stable source_id.

MANIFEST_FIELDS = ("title", "description", "competition", "event_date",
                   "participants", "source", "license")


def load_manifest(path):
    """Read a CSV manifest into {file: {field: value}}."""
    import csv

    rows = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "file" not in reader.fieldnames:
            raise MetadataError("manifest needs a 'file' column")
        for row in reader:
            name = (row.get("file") or "").strip()
            if name:
                rows[name] = {k: (row.get(k) or "").strip() for k in MANIFEST_FIELDS
                              if (row.get(k) or "").strip()}
    return rows


def _metadata_for(relpath, defaults, manifest):
    data = dict(defaults)
    override = manifest.get(relpath) or manifest.get(os.path.basename(relpath)) or {}
    data.update(override)
    return validate_metadata(data, relpath)


def _import_one(conn, report, relpath, source_id, defaults, manifest, user_id,
                size, ingest_input, max_attempts):
    if conn.execute("SELECT 1 FROM videos WHERE source_id = ?", (source_id,)).fetchone():
        report["skipped"].append((relpath, "already imported"))
        return
    try:
        meta = _metadata_for(relpath, defaults, manifest)
    except MetadataError as exc:
        report["errors"].append((relpath, str(exc)))
        return
    video_id = create_video(
        conn, user_id=user_id, meta=meta, filename=relpath, size_bytes=size,
        ingest_input=ingest_input, source_id=source_id, max_attempts=max_attempts,
    )
    conn.commit()
    report["created"].append((relpath, video_id))


def _new_report():
    return {"created": [], "skipped": [], "errors": []}


def bulk_import_folder(conn, folder, *, user_id, defaults, manifest=None,
                       recursive=True, max_attempts=3):
    """Queue every video file under `folder`. Files are read in place (never
    moved or deleted), so leave them there until their ingest job is done."""
    folder = os.path.realpath(folder)
    if not os.path.isdir(folder):
        raise MetadataError(f"folder not found: {folder}")
    manifest = manifest or {}
    report = _new_report()
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        if not recursive:
            dirs[:] = []
        for name in sorted(files):
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in IMPORT_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            relpath = os.path.relpath(path, folder).replace(os.sep, "/")
            size = os.path.getsize(path)
            _import_one(
                conn, report, relpath, f"folder:{path}:{size}", defaults, manifest,
                user_id, size,
                {"staged_path": path, "delete_staged": False, "filename": name},
                max_attempts,
            )
    return report


def bulk_import_storage(conn, storage, prefix, *, user_id, defaults, manifest=None,
                        max_attempts=3):
    """Queue every video object under `prefix` in storage; each object is
    adopted in place as that video's immutable original."""
    if not prefix.strip("/"):
        raise MetadataError("a non-empty prefix is required (e.g. 'inbox/')")
    manifest = manifest or {}
    report = _new_report()
    for key in storage.list(prefix):
        if key.startswith("videos/") or os.path.splitext(key)[1].lower() not in IMPORT_EXTENSIONS:
            continue  # videos/ holds the pipeline's own originals/derivatives
        owned = conn.execute(
            "SELECT 1 FROM videos WHERE stored_name = ? UNION ALL"
            " SELECT 1 FROM assets WHERE uri = ?", (key, key),
        ).fetchone()
        if owned:
            report["skipped"].append((key, "already belongs to a video"))
            continue
        relpath = key[len(prefix):].lstrip("/") or key
        _import_one(
            conn, report, relpath, f"storage:{key}", defaults, manifest, user_id,
            storage.size(key), {"source_key": key}, max_attempts,
        )
    return report
