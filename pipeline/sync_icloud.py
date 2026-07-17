#!/usr/bin/env python3
"""Sync videos from a local folder (e.g. iCloud downloads) to BJJ Video Hosting.

This runs on YOUR machine, not on the server. iCloud has no server-side API,
so the flow is:

    iCloud  ->  a local folder  ->  Azure Blob Storage  ->  the website's DB

The folder can be populated however you like:
  * macOS: Photos app "Export Unmodified Original", or `osxphotos export`
  * Windows: iCloud for Windows syncs to the iCloud Photos\\Downloads folder
  * Any OS: `icloudpd` (this script can drive it with --icloud)

For each new video the script uploads the file straight to your blob
container (fast, and it skips the web app's request timeout) and then calls
the site's /api/import endpoint to register it. State is tracked in a local
manifest keyed by a stable per-file id, so re-running only picks up new
videos — that's the "refresh".

Configuration (env vars, or a .env-style export):
    AZURE_STORAGE_CONNECTION_STRING   where to upload the blobs
    AZURE_CONTAINER_NAME              blob container (default: videos)
    SITE_URL                         e.g. https://bjjvidhost.azurewebsites.net
    IMPORT_API_TOKEN                 must match the server's IMPORT_API_TOKEN
    IMPORT_USERNAME                  the account that will own the videos

Examples:
    # Sync everything already downloaded to a folder
    python sync_icloud.py --source ~/Pictures/iCloudVideos

    # Download from iCloud with icloudpd first, then sync
    python sync_icloud.py --icloud --icloud-user you@icloud.com

    # See what would happen without uploading
    python sync_icloud.py --source ./clips --dry-run
"""
import argparse
import json
import logging
import mimetypes
import os
import subprocess
import sys
import tempfile
import uuid

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".ogg", ".ogv", ".avi", ".mkv"}
# Extensions the website can serve directly; others are converted when
# --compress is on, otherwise uploaded as-is (some browsers won't play them).
WEB_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".m4v", ".mov"}

log = logging.getLogger("sync")


# --- helpers ------------------------------------------------------------

def iter_video_files(directory):
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                yield os.path.join(root, name)


def source_id_for(path):
    """A stable id for a file so re-runs skip it. Filename + size is fast and
    stable across runs; content doesn't need re-hashing every time."""
    return f"icloud:{os.path.basename(path)}:{os.path.getsize(path)}"


def title_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("_", " ").replace("-", " ").strip() or stem


def load_manifest(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_manifest(path, manifest):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def compress_to_temp(src_path):
    """Transcode to a web-friendly MP4 next to a temp file. Returns the temp
    path, or None if ffmpeg is unavailable or the transcode failed."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        log.warning("ffmpeg not found; uploading %s uncompressed",
                    os.path.basename(src_path))
        return None
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    try:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src_path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
             "-vf", "scale='min(1920,iw)':-2", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out],
            check=True, capture_output=True,
        )
        return out
    except subprocess.CalledProcessError as exc:
        log.warning("ffmpeg failed for %s: %s", os.path.basename(src_path),
                    (exc.stderr or b"").decode(errors="replace")[-300:])
        _quiet_remove(out)
        return None


def thumbnail_to_temp(src_path):
    """Extract a poster JPEG. Returns the temp path, or None if unavailable."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", seek, "-i", src_path, "-frames:v", "1",
                 "-vf", "scale=640:-2", "-q:v", "3", out],
                check=True, capture_output=True,
            )
            if os.path.getsize(out) > 0:
                return out
        except (subprocess.CalledProcessError, OSError):
            continue
    _quiet_remove(out)
    return None


def _find_ffmpeg():
    import shutil

    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _quiet_remove(path):
    try:
        os.remove(path)
    except (FileNotFoundError, TypeError):
        pass


# --- client builders (wired to real Azure + HTTP in main) ---------------

def build_azure_putter(connection_string, container_name):
    from azure.storage.blob import BlobServiceClient, ContentSettings

    service = BlobServiceClient.from_connection_string(connection_string)
    container = service.get_container_client(container_name)
    if not container.exists():
        container.create_container()

    def put(local_path, stored_name, content_type):
        with open(local_path, "rb") as data:
            container.get_blob_client(stored_name).upload_blob(
                data, overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        return os.path.getsize(local_path)

    return put


def build_registrar(site_url, token):
    import requests

    endpoint = site_url.rstrip("/") + "/api/import"
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    def register(payload):
        resp = session.post(endpoint, json=payload, timeout=30)
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text[:200]}
        return resp.status_code, body

    return register


# --- core orchestration (unit-testable) ---------------------------------

def sync(source_dir, putter, registrar, manifest_path, username,
         compress=False, dry_run=False):
    """Upload+register every new video in source_dir. Returns a summary dict."""
    manifest = load_manifest(manifest_path)
    summary = {"new": 0, "skipped": 0, "failed": 0}

    for path in iter_video_files(source_dir):
        sid = source_id_for(path)
        if sid in manifest:
            summary["skipped"] += 1
            log.debug("skip (already synced): %s", path)
            continue

        log.info("new video: %s", path)
        if dry_run:
            summary["new"] += 1
            continue

        upload_path, cleanup = path, None
        ext = os.path.splitext(path)[1].lower()
        if compress or ext not in WEB_EXTENSIONS:
            converted = compress_to_temp(path)
            if converted:
                upload_path, cleanup, ext = converted, converted, ".mp4"

        stored_name = f"{uuid.uuid4().hex}{ext}"
        content_type = mimetypes.guess_type(stored_name)[0] or "video/mp4"
        try:
            size = putter(upload_path, stored_name, content_type)

            # Extract a poster and upload it alongside the video (best effort).
            thumbnail_name = None
            thumb = thumbnail_to_temp(upload_path)
            if thumb:
                thumbnail_name = stored_name.rsplit(".", 1)[0] + ".jpg"
                try:
                    putter(thumb, thumbnail_name, "image/jpeg")
                except Exception as exc:  # noqa: BLE001
                    log.warning("  thumbnail upload failed: %s", exc)
                    thumbnail_name = None
                finally:
                    _quiet_remove(thumb)

            status, body = registrar({
                "username": username,
                "stored_name": stored_name,
                "source_id": sid,
                "title": title_from_filename(path),
                "size_bytes": size,
                "content_type": content_type,
                "thumbnail_name": thumbnail_name,
            })
            if status in (200, 201):
                manifest[sid] = {"stored_name": stored_name,
                                 "video_id": body.get("id"),
                                 "result": body.get("status")}
                save_manifest(manifest_path, manifest)
                summary["new"] += 1
                log.info("  registered (%s) -> %s", body.get("status"),
                         body.get("id"))
            else:
                summary["failed"] += 1
                log.error("  import failed [%s]: %s", status, body.get("error"))
        except Exception as exc:  # noqa: BLE001 - keep going on one bad file
            summary["failed"] += 1
            log.error("  error syncing %s: %s", path, exc)
        finally:
            _quiet_remove(cleanup)

    return summary


def download_from_icloud(staging_dir, username, extra_args):
    """Drive icloudpd to download the library into staging_dir."""
    os.makedirs(staging_dir, exist_ok=True)
    cmd = ["icloudpd", "--directory", staging_dir, "--username", username,
           "--folder-structure", "none"] + list(extra_args)
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


# --- CLI ----------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--source", help="folder of videos to sync")
    src.add_argument("--icloud", action="store_true",
                     help="download from iCloud with icloudpd first")
    parser.add_argument("--icloud-user", default=os.environ.get("ICLOUD_USERNAME"),
                        help="Apple ID for --icloud")
    parser.add_argument("--icloudpd-arg", action="append", default=[],
                        help="extra arg passed through to icloudpd (repeatable)")
    parser.add_argument("--manifest", default="bjjvid_sync_manifest.json",
                        help="where to record already-synced videos")
    parser.add_argument("--compress", action="store_true",
                        help="transcode/compress each video before upload")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be synced, upload nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    missing = [v for v in ("SITE_URL", "IMPORT_API_TOKEN", "IMPORT_USERNAME")
               if not os.environ.get(v)]
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not args.dry_run and not conn_str:
        missing.append("AZURE_STORAGE_CONNECTION_STRING")
    if not args.dry_run and missing:
        parser.error("missing required env vars: " + ", ".join(missing))

    source_dir = args.source
    if args.icloud:
        if not args.icloud_user:
            parser.error("--icloud needs --icloud-user or ICLOUD_USERNAME")
        source_dir = os.path.join(tempfile.gettempdir(), "bjjvid_icloud")
        download_from_icloud(source_dir, args.icloud_user, args.icloudpd_arg)

    if not os.path.isdir(source_dir):
        parser.error(f"source folder not found: {source_dir}")

    if args.dry_run:
        putter = registrar = None  # unused in dry-run
    else:
        putter = build_azure_putter(
            conn_str, os.environ.get("AZURE_CONTAINER_NAME", "videos"))
        registrar = build_registrar(
            os.environ["SITE_URL"], os.environ["IMPORT_API_TOKEN"])

    summary = sync(
        source_dir, putter, registrar, args.manifest,
        username=os.environ.get("IMPORT_USERNAME", ""),
        compress=args.compress, dry_run=args.dry_run,
    )
    log.info("Done. new=%(new)d skipped=%(skipped)d failed=%(failed)d", summary)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
