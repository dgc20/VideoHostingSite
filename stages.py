"""Pipeline stage implementations: ingest, probe, transcode, keyframes.

Each stage is `fn(ctx, conn, video, job_input)`, called by jobs.run_job with
an open DB connection (committed by the caller on success). Stages are
idempotent so a retry is always safe: objects are written once under
versioned keys (an existing key is adopted, never overwritten — STO-1) and
asset rows are inserted with ON CONFLICT DO NOTHING.

Storage layout for a video:

    videos/<id>/original<ext>            immutable upload (or an adopted key)
    videos/<id>/mezzanine-v1.mp4         H.264 <=1080p, used for playback
    videos/<id>/proxy480-v1.mp4          H.264 480p, used for analysis
    videos/<id>/keyframes-v1/<ms>.jpg    one per shot + every N seconds
    videos/<id>/thumbnail-v1.jpg         poster frame

Local working copies live under <work_root>/<video id>/ while the pipeline
runs and are removed when it ends.
"""
import hashlib
import mimetypes
import os
import uuid

import db
import processing
from jobs import PermanentError, StopPipeline

# Extensions browsers can generally play as-is, so a small original can be
# served while the mezzanine is being made.
PLAYABLE_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v"}

STAGE_VERSIONS = {
    "ingest": "ingest-v1",
    "probe": "probe-v1",
    "transcode": processing.TRANSCODE_VERSION,
    "keyframes": processing.KEYFRAMES_VERSION,
}

MEZZANINE_NAME = "mezzanine-v1.mp4"
PROXY_NAME = "proxy480-v1.mp4"
KEYFRAME_PREFIX = "keyframes-v1"
THUMBNAIL_NAME = "thumbnail-v1.jpg"


def work_dir(ctx, video_id):
    return os.path.join(ctx.work_root, video_id)


def video_key(video_id, name):
    return f"videos/{video_id}/{name}"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_ffmpeg():
    ffmpeg = processing.find_ffmpeg()
    if not ffmpeg:
        raise StopPipeline("ffmpeg not available", status="skipped")
    return ffmpeg


def put_once(storage, local_path, key, content_type):
    """Store a local file under key unless it's already there (write-once).

    An existing key is adopted: writes are atomic, so it is a complete copy
    from an earlier attempt. Returns the stored size.
    """
    if storage.exists(key):
        return storage.size(key)
    try:
        with open(local_path, "rb") as f:
            return storage.save(f, key, content_type, overwrite=False)
    except FileExistsError:
        return storage.size(key)


def record_asset(conn, video_id, kind, uri, *, size_bytes=None, codec=None,
                 width=None, height=None, ts_ms=None, pipeline_version=None):
    conn.execute(
        "INSERT INTO assets (id, video_id, kind, uri, codec, size_bytes, width,"
        " height, ts_ms, pipeline_version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (video_id, uri) DO NOTHING",
        (uuid.uuid4().hex, video_id, kind, uri, codec, size_bytes, width, height,
         ts_ms, pipeline_version, db.utcnow()),
    )


def get_asset(conn, video_id, kind):
    return conn.execute(
        "SELECT * FROM assets WHERE video_id = ? AND kind = ?"
        " ORDER BY created_at LIMIT 1",
        (video_id, kind),
    ).fetchone()


def local_copy(ctx, video_id, key):
    """Return a local path for a stored object, downloading it if remote."""
    if not ctx.storage.is_remote:
        return ctx.storage.path(key)
    path = os.path.join(work_dir(ctx, video_id), key.rsplit("/", 1)[-1])
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        ctx.storage.download_to(key, tmp)
        os.replace(tmp, path)
    return path


def _cache_locally(ctx, video_id, key, local_path):
    """Keep a just-written output in the work dir so later stages on a
    remote backend don't download it again."""
    if ctx.storage.is_remote:
        dest = os.path.join(work_dir(ctx, video_id), key.rsplit("/", 1)[-1])
        if os.path.abspath(local_path) != os.path.abspath(dest):
            os.replace(local_path, dest)


# --- stages -----------------------------------------------------------------

def ingest(ctx, conn, video, job_input):
    """Hash, dedupe and store the original (ING-5 exact, STO-1).

    job_input is either {"staged_path", "delete_staged", "filename"} for a
    local file (upload, folder import) or {"source_key"} for an object that
    is already in storage (/api/import, storage-prefix import), which is
    adopted in place as the original.
    """
    video_id = video["id"]
    source_key = job_input.get("source_key")
    if source_key:
        if not ctx.storage.exists(source_key):
            raise PermanentError(f"stored object '{source_key}' not found")
        local = local_copy(ctx, video_id, source_key)
        filename = source_key
    else:
        local = job_input.get("staged_path")
        if not local or not os.path.exists(local):
            raise PermanentError("staged upload file is missing")
        filename = job_input.get("filename") or local
    delete_staged = bool(job_input.get("delete_staged")) and not source_key

    content_hash = sha256_file(local)
    dup = conn.execute(
        "SELECT id FROM videos WHERE content_hash = ? AND id != ?"
        " AND status != 'duplicate' ORDER BY uploaded_at LIMIT 1",
        (content_hash, video_id),
    ).fetchone()
    if dup:
        conn.execute(
            "UPDATE videos SET status = 'duplicate', duplicate_of = ?,"
            " content_hash = ? WHERE id = ?",
            (dup["id"], content_hash, video_id),
        )
        if delete_staged:
            os.remove(local)
        raise StopPipeline(f"duplicate of video {dup['id']}")

    ext = os.path.splitext(filename)[1].lower()
    content_type = mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
    if source_key:
        key, size = source_key, ctx.storage.size(source_key)
    else:
        key = video_key(video_id, f"original{ext}")
        size = put_once(ctx.storage, local, key, content_type)
    record_asset(conn, video_id, "original", key, size_bytes=size,
                 pipeline_version=STAGE_VERSIONS["ingest"])

    # Small, browser-friendly originals are watchable right away; anything
    # else waits for the mezzanine (as large uploads always did).
    playable_now = ext in PLAYABLE_EXTENSIONS and size <= ctx.compress_threshold
    conn.execute(
        "UPDATE videos SET stored_name = ?, content_type = ?, size_bytes = ?,"
        " content_hash = ?,"
        " status = CASE WHEN ? AND status = 'processing' THEN 'ready' ELSE status END"
        " WHERE id = ?",
        (key, content_type, size, content_hash, 1 if playable_now else 0, video_id),
    )
    if delete_staged:
        if ctx.storage.is_remote:
            os.makedirs(work_dir(ctx, video_id), exist_ok=True)
            _cache_locally(ctx, video_id, key, local)
        else:
            os.remove(local)


def probe(ctx, conn, video, _job_input):
    """Record duration, fps and resolution of the original."""
    ffmpeg = _require_ffmpeg()
    original = get_asset(conn, video["id"], "original")
    if original is None:
        raise PermanentError("no original asset")
    try:
        info = processing.probe(ffmpeg, local_copy(ctx, video["id"], original["uri"]))
    except processing.ProbeError as exc:
        raise PermanentError(f"not a readable video: {exc}") from None
    conn.execute(
        "UPDATE videos SET duration_ms = ?, fps = ?, width = ?, height = ?"
        " WHERE id = ?",
        (info["duration_ms"], info["fps"], info["width"], info["height"], video["id"]),
    )
    conn.execute(
        "UPDATE assets SET codec = ?, width = ?, height = ? WHERE id = ?",
        (info["codec"], info["width"], info["height"], original["id"]),
    )


def transcode(ctx, conn, video, _job_input):
    """PRE-1: mezzanine (H.264, <=1080p) + 480p proxy; play the mezzanine."""
    ffmpeg = _require_ffmpeg()
    video_id = video["id"]
    original = get_asset(conn, video_id, "original")
    src = local_copy(ctx, video_id, original["uri"])
    wd = work_dir(ctx, video_id)
    os.makedirs(wd, exist_ok=True)
    mezz_key = video_key(video_id, MEZZANINE_NAME)
    proxy_key = video_key(video_id, PROXY_NAME)
    if ctx.storage.exists(mezz_key) and ctx.storage.exists(proxy_key):
        # A previous attempt stored both; reuse rather than re-encode.
        mezz_local = local_copy(ctx, video_id, mezz_key)
        proxy_local = local_copy(ctx, video_id, proxy_key)
    else:
        mezz_local = os.path.join(wd, "tmp-" + MEZZANINE_NAME)
        proxy_local = os.path.join(wd, "tmp-" + PROXY_NAME)
        processing.transcode_renditions(ffmpeg, src, mezz_local, proxy_local)

    sizes = {}
    for kind, key, local in (("mezzanine", mezz_key, mezz_local),
                             ("proxy", proxy_key, proxy_local)):
        info = processing.probe(ffmpeg, local)
        sizes[kind] = put_once(ctx.storage, local, key, "video/mp4")
        record_asset(conn, video_id, kind, key, size_bytes=sizes[kind],
                     codec=info["codec"], width=info["width"],
                     height=info["height"],
                     pipeline_version=processing.TRANSCODE_VERSION)
        if os.path.basename(local).startswith("tmp-"):
            if ctx.storage.is_remote:
                _cache_locally(ctx, video_id, key, local)
            else:
                os.remove(local)
    conn.execute(
        "UPDATE videos SET stored_name = ?, content_type = 'video/mp4',"
        " size_bytes = ?, status = 'ready' WHERE id = ?",
        (mezz_key, sizes["mezzanine"], video_id),
    )


def keyframes(ctx, conn, video, _job_input):
    """PRE-3: keyframes from the proxy, plus the poster thumbnail."""
    ffmpeg = _require_ffmpeg()
    video_id = video["id"]
    proxy = get_asset(conn, video_id, "proxy")
    src = local_copy(ctx, video_id, proxy["uri"])
    out_dir = os.path.join(work_dir(ctx, video_id), "keyframes")
    frames = processing.extract_keyframes(
        ffmpeg, src, out_dir,
        interval_s=ctx.keyframe_interval_s,
        scene_threshold=ctx.keyframe_scene_threshold,
    )
    for ts_ms, path in frames:
        key = video_key(video_id, f"{KEYFRAME_PREFIX}/{ts_ms:09d}.jpg")
        size = put_once(ctx.storage, path, key, "image/jpeg")
        record_asset(conn, video_id, "keyframe", key, size_bytes=size,
                     ts_ms=ts_ms, pipeline_version=processing.KEYFRAMES_VERSION)
        os.remove(path)

    # Poster thumbnail from the mezzanine (sharper than the proxy).
    mezz = get_asset(conn, video_id, "mezzanine")
    thumb_local = os.path.join(work_dir(ctx, video_id), THUMBNAIL_NAME)
    if processing.make_thumbnail(ffmpeg, local_copy(ctx, video_id, mezz["uri"]),
                                 thumb_local):
        key = video_key(video_id, THUMBNAIL_NAME)
        size = put_once(ctx.storage, thumb_local, key, "image/jpeg")
        record_asset(conn, video_id, "thumbnail", key, size_bytes=size,
                     pipeline_version=processing.KEYFRAMES_VERSION)
        conn.execute(
            "UPDATE videos SET thumbnail_name = ? WHERE id = ?", (key, video_id)
        )


STAGE_FUNCTIONS = {
    "ingest": ingest,
    "probe": probe,
    "transcode": transcode,
    "keyframes": keyframes,
}


def on_pipeline_end(ctx, conn, video_id):
    """Settle the video's status once no stage is queued or running.

    If transcoding never happened (ffmpeg missing or it couldn't decode the
    file) the original stays playable, as uploads always were; if the
    original itself never made it into storage the video has failed.
    """
    video = conn.execute(
        "SELECT status FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if video is None or video["status"] != "processing":
        return
    if get_asset(conn, video_id, "original") is not None:
        conn.execute("UPDATE videos SET status = 'ready' WHERE id = ?", (video_id,))
    else:
        conn.execute("UPDATE videos SET status = 'failed' WHERE id = ?", (video_id,))


def delete_video_files(storage, conn, video):
    """Delete every stored object for a video (owner-requested deletion).

    Keys adopted from outside videos/<id>/ (via /api/import) are deleted too,
    matching the previous behaviour of deleting the stored file.
    """
    keys = {row["uri"] for row in conn.execute(
        "SELECT uri FROM assets WHERE video_id = ?", (video["id"],)
    )}
    for legacy in (video["stored_name"], video["thumbnail_name"]):
        if legacy:
            keys.add(legacy)
    for key in keys:
        try:
            storage.delete(key)
        except ValueError:
            pass  # not a valid key (e.g. the '' placeholder)
