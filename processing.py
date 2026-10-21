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
import glob
import logging
import os
import re
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


# --- Analysis pipeline primitives (PRE-1, PRE-3) ---------------------------
#
# Pure ffmpeg wrappers: local file in, local files out. The job stages in
# stages.py handle storage and the database around them. imageio-ffmpeg ships
# ffmpeg but not ffprobe, so probe() parses `ffmpeg -i` output.

# Bump when an output's encoding settings change, so reprocessed derivatives
# land under new keys instead of overwriting immutable ones.
TRANSCODE_VERSION = "transcode-v1"
KEYFRAMES_VERSION = "keyframes-v1"

_DURATION_RE = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"([\d.]+) fps")
_ROTATION_RE = re.compile(r"rotation of (-?[\d.]+) degrees|rotate\s*:\s*(-?\d+)")


class ProbeError(Exception):
    """ffmpeg could not read the file as a video."""


def probe(ffmpeg, path):
    """Return {duration_ms, fps, width, height, codec} for a video file.

    Width/height are as displayed (swapped for 90-degree rotated phone
    footage). Raises ProbeError if no video stream is found.
    """
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path], capture_output=True
    )
    # `ffmpeg -i` with no output always exits 1; the info is on stderr.
    text = result.stderr.decode(errors="replace")
    video = _VIDEO_RE.search(text)
    if not video:
        raise ProbeError(text.strip().splitlines()[-1] if text.strip() else "no output")
    info = {
        "codec": video.group(1),
        "width": int(video.group(2)),
        "height": int(video.group(3)),
        "duration_ms": None,
        "fps": None,
    }
    duration = _DURATION_RE.search(text)
    if duration:
        h, m, sec = duration.groups()
        info["duration_ms"] = int(round((int(h) * 3600 + int(m) * 60 + float(sec)) * 1000))
    line = text[video.start():].splitlines()[0]
    fps = _FPS_RE.search(line)
    if fps:
        info["fps"] = float(fps.group(1))
    rotation = _ROTATION_RE.search(text)
    if rotation:
        degrees = abs(float(rotation.group(1) or rotation.group(2))) % 180
        if degrees == 90:
            info["width"], info["height"] = info["height"], info["width"]
    return info


def transcode_renditions(ffmpeg, src_path, mezzanine_path, proxy_path):
    """PRE-1: one decode, two outputs.

    - mezzanine: H.264/AAC, fit inside 1920x1080 (never upscaled), CRF 23
    - proxy: H.264/AAC at 480 lines (never upscaled), CRF 30, for analysis
    Both are MP4 with faststart so they stream and seek well.
    """
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path,
            # Mezzanine
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)'"
                   ":force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            mezzanine_path,
            # Proxy
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=-2:'min(480,ih)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart",
            proxy_path,
        ],
        check=True,
        capture_output=True,
    )


_SHOWINFO_RE = re.compile(r"Parsed_showinfo.*?pts_time:\s*(-?[\d.]+)")


def extract_keyframes(ffmpeg, src_path, out_dir, interval_s=10,
                      scene_threshold=0.4, width=512):
    """PRE-3: keyframes at every shot change plus at least every interval_s.

    A single ffmpeg pass selects a frame when it is the first frame, when the
    scene-change score exceeds scene_threshold (a hard cut), or when
    interval_s seconds have passed since the last selected frame. Frames are
    written as JPEGs `width` px wide. Returns [(ts_ms, path), ...] in order.
    """
    os.makedirs(out_dir, exist_ok=True)
    select = (
        "select='isnan(prev_selected_t)"
        f"+gte(t-prev_selected_t\\,{float(interval_s)})"
        f"+gt(scene\\,{float(scene_threshold)})'"
    )
    pattern = os.path.join(out_dir, "kf_%06d.jpg")
    result = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "info",
            "-i", src_path, "-an",
            "-vf", f"{select},showinfo,scale={int(width)}:-2",
            "-fps_mode", "vfr", "-q:v", "3",
            pattern,
        ],
        check=True,
        capture_output=True,
    )
    times = [
        float(t) for t in _SHOWINFO_RE.findall(result.stderr.decode(errors="replace"))
    ]
    files = sorted(glob.glob(os.path.join(out_dir, "kf_*.jpg")))
    if len(times) != len(files):
        raise RuntimeError(
            f"keyframe timestamps ({len(times)}) and files ({len(files)}) disagree"
        )
    return [(int(round(t * 1000)), path) for t, path in zip(times, files)]


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
    """Recover legacy 'processing' videos after a restart.

    If the raw upload still exists in incoming_dir, restart its compression;
    otherwise the bytes are gone and the video is marked failed.
    """
    conn = sqlite3.connect(database)
    try:
        # Videos with processing_jobs rows belong to the job pipeline
        # (jobs.py), which resumes them itself.
        rows = conn.execute(
            "SELECT id FROM videos WHERE status = 'processing' AND id NOT IN"
            " (SELECT video_id FROM processing_jobs)"
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
