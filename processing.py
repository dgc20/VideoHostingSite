"""Background video compression.

Uploads over the compression threshold are transcoded off-request by a
daemon thread: H.264/AAC in an MP4 container, capped at 1080p width, CRF 28.
That combination typically shrinks phone/4K footage by 5-10x while staying
sharp enough for technique video.

ffmpeg comes from the system PATH when available, otherwise from the
imageio-ffmpeg package, which bundles a static binary that pip installs —
this is what makes compression work on Azure App Service, where you can't
apt-get install anything persistently.

If ffmpeg is unavailable or the transcode fails (corrupt input, unsupported
codec), the original file is stored unmodified rather than failing the
upload; 'failed' status is reserved for videos whose bytes were lost.
"""
import logging
import os
import shutil
import sqlite3
import subprocess
import threading

log = logging.getLogger(__name__)


def find_ffmpeg():
    """Return a path to an ffmpeg executable, or None."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def compress(ffmpeg, in_path, out_path):
    """Transcode in_path to a web-friendly compressed MP4 at out_path."""
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", in_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            # Cap width at 1920 (1080p-class); -2 keeps aspect ratio and
            # even dimensions. Smaller videos pass through unscaled.
            "-vf", "scale='min(1920,iw)':-2",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            out_path,
        ],
        check=True,
        capture_output=True,
    )


def make_thumbnail(ffmpeg, src_path, out_path):
    """Grab a single frame as a JPEG poster. Returns True on success.

    Seeks ~1s in for a representative frame; retries from the start for very
    short clips where 1s is past the end.
    """
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", seek, "-i", src_path, "-frames:v", "1",
                 "-vf", "scale=640:-2", "-q:v", "3", out_path],
                check=True, capture_output=True,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
        except subprocess.CalledProcessError:
            continue
    return False


def store_thumbnail(storage, video_id, video_path):
    """Extract a poster from video_path and store it. Returns its stored name
    (``<video_id>.jpg``) or None if ffmpeg is missing / extraction failed."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    tmp = f"{video_path}.thumb.jpg"
    try:
        if not make_thumbnail(ffmpeg, video_path, tmp):
            log.warning("could not extract thumbnail for %s", video_id)
            return None
        name = f"{video_id}.jpg"
        with open(tmp, "rb") as f:
            if storage.is_remote:
                storage.save(f, name, "image/jpeg")
            else:
                storage.save(f, name)
        return name
    except Exception:
        log.exception("thumbnail storage failed for %s", video_id)
        return None
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def process_async(database, storage, video_id, raw_path):
    """Compress raw_path and finalize the video row, on a daemon thread."""
    thread = threading.Thread(
        target=_process, args=(database, storage, video_id, raw_path), daemon=True
    )
    thread.start()
    return thread


def _process(database, storage, video_id, raw_path):
    out_path = f"{raw_path}.out.mp4"
    try:
        upload_path = raw_path
        stored_name = f"{video_id}{os.path.splitext(raw_path)[1]}"
        content_type_map = {".mp4": "video/mp4", ".webm": "video/webm",
                            ".ogg": "video/ogg", ".ogv": "video/ogg",
                            ".mov": "video/quicktime", ".m4v": "video/x-m4v"}
        content_type = content_type_map.get(
            os.path.splitext(raw_path)[1].lower(), "application/octet-stream"
        )

        ffmpeg = find_ffmpeg()
        if ffmpeg:
            try:
                compress(ffmpeg, raw_path, out_path)
                # Keep the compressed copy only if it actually saved space.
                if os.path.getsize(out_path) < os.path.getsize(raw_path):
                    upload_path = out_path
                    stored_name = f"{video_id}.mp4"
                    content_type = "video/mp4"
            except subprocess.CalledProcessError as exc:
                log.warning(
                    "ffmpeg failed for %s, storing original: %s",
                    video_id,
                    (exc.stderr or b"").decode(errors="replace")[-500:],
                )
        else:
            log.warning("ffmpeg not found; storing %s uncompressed", video_id)

        size = os.path.getsize(upload_path)
        with open(upload_path, "rb") as f:
            if storage.is_remote:
                storage.save(f, stored_name, content_type)
            else:
                storage.save(f, stored_name)

        thumbnail_name = store_thumbnail(storage, video_id, upload_path)

        conn = sqlite3.connect(database)
        try:
            row = conn.execute(
                "SELECT 1 FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if row is None:
                # Deleted while processing; discard the stored files.
                storage.delete(stored_name)
                if thumbnail_name:
                    storage.delete(thumbnail_name)
                return
            conn.execute(
                "UPDATE videos SET stored_name = ?, content_type = ?,"
                " size_bytes = ?, thumbnail_name = ?, status = 'ready'"
                " WHERE id = ?",
                (stored_name, content_type, size, thumbnail_name, video_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("processing failed for video %s", video_id)
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "UPDATE videos SET status = 'failed' WHERE id = ?", (video_id,)
            )
            conn.commit()
        finally:
            conn.close()
    finally:
        for path in (raw_path, out_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def resume_pending(database, storage, incoming_dir):
    """Recover 'processing' videos after a restart.

    If the raw upload still exists in incoming_dir, restart its compression;
    otherwise the bytes are gone and the video is marked failed.
    """
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute(
            "SELECT id FROM videos WHERE status = 'processing'"
        ).fetchall()
        for (video_id,) in rows:
            raw = next(
                (
                    os.path.join(incoming_dir, name)
                    for name in os.listdir(incoming_dir)
                    if name.startswith(f"raw_{video_id}")
                    and not name.endswith(".out.mp4")
                ),
                None,
            ) if os.path.isdir(incoming_dir) else None
            if raw:
                log.info("resuming compression for %s", video_id)
                process_async(database, storage, video_id, raw)
            else:
                log.warning("raw upload for %s lost; marking failed", video_id)
                conn.execute(
                    "UPDATE videos SET status = 'failed' WHERE id = ?",
                    (video_id,),
                )
        conn.commit()
    finally:
        conn.close()
