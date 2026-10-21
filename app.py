"""A simple video hosting website.

Run locally:
    pip install -r requirements.txt
    flask --app app run --debug

In production (Azure App Service):
    gunicorn --bind=0.0.0.0:8000 --timeout 600 app:app
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

import auth
import db
import ingest
import jobs
import processing
import stages
import storage as storage_module

ALLOWED_EXTENSIONS = ingest.ALLOWED_EXTENSIONS
# BJJ adult belt progression, in order.
BELTS = ("white", "blue", "purple", "brown", "black")

# Azure App Service persists /home across restarts; use it when available so
# the SQLite database and local uploads survive redeploys.
_default_data_dir = "/home/data" if os.path.isdir("/home") and os.access("/home", os.W_OK) else "instance"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024,
    # Uploads larger than this are compressed in the background with ffmpeg.
    COMPRESS_THRESHOLD=int(os.environ.get("COMPRESS_THRESHOLD_MB", "500"))
    * 1024
    * 1024,
    DATA_DIR=os.environ.get("DATA_DIR", _default_data_dir),
    AZURE_STORAGE_CONNECTION_STRING=os.environ.get("AZURE_STORAGE_CONNECTION_STRING"),
    AZURE_CONTAINER_NAME=os.environ.get("AZURE_CONTAINER_NAME", "videos"),
    # Session cookie hardening. SECURE defaults off so local http works;
    # set SESSION_COOKIE_SECURE=1 in production (Azure serves over HTTPS).
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    # Bearer token for the /api/import endpoint used by the iCloud pipeline.
    # When unset, the endpoint is disabled.
    IMPORT_API_TOKEN=os.environ.get("IMPORT_API_TOKEN"),
    # --- Analysis pipeline (Phase 0) ---
    # Run the background job worker in this process ("0" disables it, e.g.
    # when jobs are drained with `flask run-jobs` instead).
    JOB_WORKER=os.environ.get("JOB_WORKER", "1") == "1",
    JOB_MAX_ATTEMPTS=int(os.environ.get("JOB_MAX_ATTEMPTS", "3")),
    JOB_LEASE_SECONDS=int(os.environ.get("JOB_LEASE_SECONDS", "120")),
    KEYFRAME_INTERVAL_S=float(os.environ.get("KEYFRAME_INTERVAL_S", "10")),
    KEYFRAME_SCENE_THRESHOLD=float(os.environ.get("KEYFRAME_SCENE_THRESHOLD", "0.4")),
    # Resumable uploads (ING-1): total size cap and per-request chunk cap.
    MAX_RESUMABLE_UPLOAD_BYTES=int(float(os.environ.get("MAX_RESUMABLE_UPLOAD_GB", "20"))
                                   * 1024 * 1024 * 1024),
    UPLOAD_CHUNK_BYTES=int(os.environ.get("UPLOAD_CHUNK_MB", "8")) * 1024 * 1024,
    UPLOAD_SESSION_TTL_HOURS=int(os.environ.get("UPLOAD_SESSION_TTL_HOURS", "48")),
    # Comma-separated usernames who see every job on the dashboard.
    ADMIN_USERNAMES={
        u.strip() for u in os.environ.get("ADMIN_USERNAMES", "").split(",") if u.strip()
    },
)
os.makedirs(app.config["DATA_DIR"], exist_ok=True)
app.config["DATABASE"] = os.path.join(app.config["DATA_DIR"], "videos.db")
app.config["UPLOAD_DIR"] = os.path.join(app.config["DATA_DIR"], "uploads")
# Uploads land here first; large ones stay while compression runs.
app.config["INCOMING_DIR"] = os.path.join(app.config["DATA_DIR"], "incoming")
os.makedirs(app.config["INCOMING_DIR"], exist_ok=True)

db.init_db(app)
storage = storage_module.create_storage(app.config)

# Restart any legacy compressions that were interrupted by a redeploy/restart
# (videos uploaded before the job pipeline existed).
processing.resume_pending(
    app.config["DATABASE"], storage, app.config["INCOMING_DIR"]
)

pipeline = jobs.PipelineContext(
    database=app.config["DATABASE"],
    storage=storage,
    work_root=os.path.join(app.config["INCOMING_DIR"], "work"),
    compress_threshold=app.config["COMPRESS_THRESHOLD"],
    keyframe_interval_s=app.config["KEYFRAME_INTERVAL_S"],
    keyframe_scene_threshold=app.config["KEYFRAME_SCENE_THRESHOLD"],
    max_attempts=app.config["JOB_MAX_ATTEMPTS"],
    lease_s=app.config["JOB_LEASE_SECONDS"],
)


@app.before_request
def _ensure_worker():
    """Start the background job worker with the first web request.

    Started lazily rather than at import so CLI commands (`flask
    import-folder`, `flask run-jobs`) don't spawn a worker that dies with the
    command mid-job. Jobs left queued or running by a previous process are
    picked up here (running ones once their lease expires).
    """
    if app.config["JOB_WORKER"]:
        jobs.start_worker(pipeline)

# Load the logged-in user and enforce CSRF on every request.
app.before_request(auth.load_logged_in_user)
app.before_request(auth.check_csrf)


@app.context_processor
def inject_globals():
    return {
        "current_user": g.get("user"),
        "csrf_token": auth.get_csrf_token,
        "BELTS": BELTS,
        "license_suggestions": ingest.LICENSE_SUGGESTIONS,
    }


def _allowed(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def _safe_next(target):
    """Only allow same-site relative redirect targets (prevents open redirect)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        error = auth.validate_registration(username, email, password)
        conn = db.get_db()
        if error is None:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE username = ? OR email = ?",
                (username, email),
            ).fetchone()
            if existing:
                error = "That username or email is already taken."

        if error:
            flash(error, "error")
            return render_template("signup.html", username=username, email=email)

        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash)"
            " VALUES (?, ?, ?, ?)",
            (user_id, username, email, auth.hash_password(password)),
        )
        conn.commit()
        auth.login_user(user_id)
        flash("Welcome to BJJ Video Hosting!", "success")
        return redirect(url_for("index"))

    return render_template("signup.html", username="", email="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = db.get_db().execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier.lower()),
        ).fetchone()

        if user and auth.verify_password(user["password_hash"], password):
            auth.login_user(user["id"])
            flash("Signed in.", "success")
            return redirect(_safe_next(request.args.get("next")) or url_for("index"))

        flash("Invalid username/email or password.", "error")
        return render_template("login.html", identifier=identifier)

    return render_template("login.html", identifier="")


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("index"))


@app.route("/api/import", methods=["POST"])
def api_import():
    """Register an already-uploaded blob as a video (used by the pipeline).

    The pipeline uploads the file straight to blob storage, then POSTs the
    metadata here. Authenticated with a bearer token; idempotent per
    source_id so the pipeline can be re-run to pick up only new videos.
    """
    token = app.config.get("IMPORT_API_TOKEN")
    if not token:
        abort(404)  # feature disabled when no token is configured
    header = request.headers.get("Authorization", "")
    provided = header[7:] if header.startswith("Bearer ") else ""
    if not provided or not secrets.compare_digest(provided, token):
        abort(401)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    stored_name = (data.get("stored_name") or "").strip()
    source_id = (data.get("source_id") or "").strip()
    thumbnail_name = (data.get("thumbnail_name") or "").strip() or None

    if not username or not stored_name or not source_id:
        return jsonify(error="username, stored_name and source_id are required"), 400
    # Stored names must be bare blob names — never paths.
    for value in (stored_name, thumbnail_name):
        if value and ("/" in value or "\\" in value or ".." in value):
            return jsonify(error="invalid stored_name"), 400
    try:
        meta = ingest.validate_metadata(data, stored_name)
    except ingest.MetadataError as exc:
        return jsonify(error=str(exc)), 400

    conn = db.get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()
    if user is None:
        return jsonify(error=f"unknown user '{username}'"), 400

    existing = conn.execute(
        "SELECT id FROM videos WHERE source_id = ?", (source_id,)
    ).fetchone()
    if existing:
        return jsonify(id=existing["id"], status="exists"), 200

    if not storage.exists(stored_name):
        return jsonify(
            error=f"no stored file named '{stored_name}' — upload it first"
        ), 409

    try:
        size = int(data.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0

    try:
        # The uploaded blob is adopted in place as the video's original and
        # goes through the same pipeline as any upload.
        video_id = ingest.create_video(
            conn, user_id=user["id"], meta=meta, filename=stored_name,
            size_bytes=size, ingest_input={"source_key": stored_name},
            source_id=source_id, max_attempts=app.config["JOB_MAX_ATTEMPTS"],
        )
        if thumbnail_name and storage.exists(thumbnail_name):
            # Show the pipeline's poster until our own keyframes are ready;
            # tracked as an asset so deleting the video removes it.
            conn.execute("UPDATE videos SET thumbnail_name = ? WHERE id = ?",
                         (thumbnail_name, video_id))
            stages.record_asset(conn, video_id, "thumbnail", thumbnail_name,
                                pipeline_version="icloud-pipeline")
        conn.commit()
    except sqlite3.IntegrityError:
        # Concurrent import of the same source_id won the race.
        conn.rollback()
        row = conn.execute(
            "SELECT id FROM videos WHERE source_id = ?", (source_id,)
        ).fetchone()
        return jsonify(id=row["id"], status="exists"), 200
    jobs.notify()
    return jsonify(id=video_id, status="created"), 201


@app.route("/account")
@auth.login_required
def account():
    videos = db.get_db().execute(
        "SELECT * FROM videos WHERE user_id = ? ORDER BY uploaded_at DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template("account.html", videos=videos)


@app.route("/account/belt", methods=["POST"])
@auth.login_required
def set_belt():
    belt = request.form.get("belt", "").strip().lower()
    if belt not in BELTS:
        flash("Please pick a valid belt.", "error")
        return redirect(url_for("account"))
    conn = db.get_db()
    conn.execute("UPDATE users SET belt = ? WHERE id = ?", (belt, g.user["id"]))
    conn.commit()
    flash(f"Belt updated to {belt}.", "success")
    return redirect(url_for("account"))


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    conn = db.get_db()
    base = (
        "SELECT v.*, u.username AS uploader, u.belt AS uploader_belt"
        " FROM videos v LEFT JOIN users u ON u.id = v.user_id"
    )
    # Exact duplicates are kept for the record but not listed.
    base += " WHERE v.status != 'duplicate'"
    if q:
        videos = conn.execute(
            base + " AND (v.title LIKE ? OR v.description LIKE ?"
            " OR v.competition LIKE ? OR v.id IN (SELECT video_id FROM"
            " video_participants WHERE name LIKE ? OR team LIKE ?))"
            " ORDER BY v.uploaded_at DESC",
            (f"%{q}%",) * 5,
        ).fetchall()
    else:
        videos = conn.execute(base + " ORDER BY v.uploaded_at DESC").fetchall()
    return render_template("index.html", videos=videos, q=q)


def _upload_form_data():
    return {key: request.form.get(key, "") for key in (
        "title", "description", "competition", "event_date", "participants",
        "source", "license",
    )}


@app.route("/upload", methods=["GET", "POST"])
@auth.login_required
def upload():
    if request.method == "POST":
        file = request.files.get("video")
        form = _upload_form_data()

        if not file or not file.filename:
            flash("Please choose a video file.", "error")
            return render_template("upload.html", form=form), 400
        if not _allowed(file.filename):
            flash(
                "Unsupported file type. Allowed: "
                + ", ".join(sorted(ALLOWED_EXTENSIONS)),
                "error",
            )
            return render_template("upload.html", form=form), 400
        try:
            meta = ingest.validate_metadata(
                form, secure_filename(file.filename) or "video"
            )
        except ingest.MetadataError as exc:
            flash(str(exc).capitalize() + ".", "error")
            return render_template("upload.html", form=form), 400

        # Land the upload on disk; the ingest job hashes it and moves it
        # into storage, then the rest of the pipeline runs in the background.
        ext = os.path.splitext(file.filename)[1].lower()
        raw_path = os.path.join(
            app.config["INCOMING_DIR"], f"upload_{uuid.uuid4().hex}{ext}"
        )
        file.save(raw_path)
        conn = db.get_db()
        video_id = ingest.create_video(
            conn,
            user_id=g.user["id"],
            meta=meta,
            filename=file.filename,
            size_bytes=os.path.getsize(raw_path),
            ingest_input={"staged_path": raw_path, "delete_staged": True,
                          "filename": file.filename},
            max_attempts=app.config["JOB_MAX_ATTEMPTS"],
        )
        conn.commit()
        jobs.notify()
        flash(
            "Video uploaded — it's being processed and will be ready to"
            " watch shortly.",
            "success",
        )
        return redirect(url_for("watch", video_id=video_id))

    return render_template("upload.html", form={"license": "own-footage"})


# --- Resumable uploads (ING-1); protocol described in ingest.py -------------

def _json_login_required(view):
    import functools

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return jsonify(error="login required"), 401
        return view(*args, **kwargs)

    return wrapped


def _upload_error(exc):
    return jsonify(error=str(exc), **exc.extra), exc.status


def _upload_payload(session_row):
    body = ingest.upload_status(session_row)
    body["upload_url"] = url_for("resumable_upload", upload_id=session_row["id"])
    if session_row["video_id"]:
        body["watch_url"] = url_for("watch", video_id=session_row["video_id"])
    return body


@app.route("/uploads", methods=["POST"])
@_json_login_required
def create_resumable_upload():
    conn = db.get_db()
    ingest.expire_upload_sessions(
        conn, app.config["INCOMING_DIR"], app.config["UPLOAD_SESSION_TTL_HOURS"]
    )
    try:
        upload_id = ingest.create_upload_session(
            conn,
            user_id=g.user["id"],
            data=request.get_json(silent=True) or {},
            max_bytes=app.config["MAX_RESUMABLE_UPLOAD_BYTES"],
            incoming_dir=app.config["INCOMING_DIR"],
            free_bytes=shutil.disk_usage(app.config["INCOMING_DIR"]).free,
        )
    except ingest.UploadError as exc:
        return _upload_error(exc)
    conn.commit()
    session_row = ingest.get_upload_session(conn, upload_id, g.user["id"])
    body = _upload_payload(session_row)
    body["chunk_size"] = app.config["UPLOAD_CHUNK_BYTES"]
    return jsonify(body), 201


@app.route("/uploads/<upload_id>", methods=["GET", "PUT", "DELETE"])
@_json_login_required
def resumable_upload(upload_id):
    conn = db.get_db()
    try:
        session_row = ingest.get_upload_session(conn, upload_id, g.user["id"])
        if request.method == "GET":
            return jsonify(_upload_payload(session_row))
        if request.method == "DELETE":
            ingest.abort_upload(conn, session_row, app.config["INCOMING_DIR"])
            conn.commit()
            return jsonify(_upload_payload(
                ingest.get_upload_session(conn, upload_id, g.user["id"])))
        session_row = ingest.write_chunk(
            conn, session_row, request.headers.get("Content-Range"),
            request.stream,
            max_chunk=app.config["UPLOAD_CHUNK_BYTES"],
            incoming_dir=app.config["INCOMING_DIR"],
            max_attempts=app.config["JOB_MAX_ATTEMPTS"],
        )
    except ingest.UploadError as exc:
        return _upload_error(exc)
    if session_row["status"] == "complete":
        jobs.notify()
        return jsonify(_upload_payload(session_row)), 201
    return jsonify(_upload_payload(session_row))


# --- Job dashboard (OPS-1) ---------------------------------------------------

def _is_admin():
    return g.user is not None and g.user["username"] in app.config["ADMIN_USERNAMES"]


@app.route("/jobs")
@auth.login_required
def jobs_dashboard():
    state = request.args.get("state", "all")
    if state not in jobs.DASHBOARD_FILTERS:
        state = "all"
    video_id = request.args.get("video") or None
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 50
    admin = _is_admin()
    videos, totals = jobs.dashboard(
        db.get_db(),
        user_id=None if admin else g.user["id"],
        state=state, video_id=video_id,
        limit=per_page + 1, offset=(page - 1) * per_page,
    )
    has_next = len(videos) > per_page
    videos = videos[:per_page]
    if request.args.get("format") == "json":
        return jsonify(
            totals=totals,
            videos=[{
                "video_id": v["video"]["id"], "title": v["video"]["title"],
                "owner": v["video"]["username"], "state": v["state"],
                "stages": [dict(s) if s is not None else None for s in v["stages"]],
            } for v in videos],
        )
    busy = any(v["state"] in ("queued", "running") for v in videos)
    return render_template(
        "jobs.html", videos=videos, totals=totals, stages=jobs.STAGES,
        state=state, filters=jobs.DASHBOARD_FILTERS, video_id=video_id,
        page=page, has_next=has_next, admin=admin, auto_refresh=busy,
    )


@app.route("/jobs/<job_id>/retry", methods=["POST"])
@auth.login_required
def retry_job(job_id):
    conn = db.get_db()
    job = conn.execute(
        "SELECT j.id, v.user_id FROM processing_jobs j JOIN videos v"
        " ON v.id = j.video_id WHERE j.id = ?", (job_id,)
    ).fetchone()
    if job is None:
        abort(404)
    if job["user_id"] != g.user["id"] and not _is_admin():
        abort(403)
    if jobs.retry(conn, job_id) is None:
        flash("That job isn't failed, so there's nothing to retry.", "error")
    else:
        conn.commit()
        jobs.notify()
        flash("Retrying.", "success")
    return redirect(_safe_next(request.form.get("next")) or url_for("jobs_dashboard"))


@app.route("/watch/<video_id>")
def watch(video_id):
    conn = db.get_db()
    video = conn.execute(
        "SELECT v.*, u.username AS uploader, u.belt AS uploader_belt"
        " FROM videos v LEFT JOIN users u ON u.id = v.user_id WHERE v.id = ?",
        (video_id,),
    ).fetchone()
    if video is None:
        abort(404)

    src = None
    if video["status"] == "ready":
        conn.execute(
            "UPDATE videos SET views = views + 1 WHERE id = ?", (video_id,)
        )
        conn.commit()
        if storage.is_remote:
            src = storage.playback_url(video["stored_name"])
        else:
            src = url_for("media", video_id=video_id)
    is_owner = g.user is not None and g.user["id"] == video["user_id"]
    participants = ingest.participants_for(conn, video_id)
    poster = url_for("thumb", video_id=video_id) if video["thumbnail_name"] else None
    comments = conn.execute(
        "SELECT c.id, c.body, c.created_at, c.user_id, u.username, u.belt"
        " FROM comments c JOIN users u ON u.id = c.user_id"
        " WHERE c.video_id = ? ORDER BY c.created_at DESC, c.rowid DESC",
        (video_id,),
    ).fetchall()
    return render_template(
        "watch.html", video=video, src=src, poster=poster,
        is_owner=is_owner, comments=comments, participants=participants
    )


@app.route("/thumb/<video_id>")
def thumb(video_id):
    """Serve a video's poster image (local file) or redirect to its SAS URL."""
    row = db.get_db().execute(
        "SELECT thumbnail_name FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if row is None or not row["thumbnail_name"]:
        abort(404)
    name = row["thumbnail_name"]
    if storage.is_remote:
        return redirect(storage.playback_url(name))
    path = storage.path(name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True)


@app.route("/watch/<video_id>/comment", methods=["POST"])
@auth.login_required
def add_comment(video_id):
    conn = db.get_db()
    exists = conn.execute(
        "SELECT 1 FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if exists is None:
        abort(404)

    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment can't be empty.", "error")
    elif len(body) > 2000:
        flash("Comment is too long (2000 characters max).", "error")
    else:
        conn.execute(
            "INSERT INTO comments (id, video_id, user_id, body)"
            " VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, video_id, g.user["id"], body),
        )
        conn.commit()
    return redirect(url_for("watch", video_id=video_id) + "#comments")


@app.route("/comment/<comment_id>/delete", methods=["POST"])
@auth.login_required
def delete_comment(comment_id):
    conn = db.get_db()
    comment = conn.execute(
        "SELECT * FROM comments WHERE id = ?", (comment_id,)
    ).fetchone()
    if comment is None:
        abort(404)
    if comment["user_id"] != g.user["id"]:
        abort(403)
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    return redirect(url_for("watch", video_id=comment["video_id"]) + "#comments")


@app.route("/media/<video_id>")
def media(video_id):
    """Serve a locally stored video with HTTP Range support for seeking."""
    if storage.is_remote:
        abort(404)
    conn = db.get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if video is None or video["status"] != "ready":
        abort(404)

    path = storage.path(video["stored_name"])
    if not os.path.exists(path):
        abort(404)

    range_header = request.headers.get("Range")
    if not range_header:
        return send_file(path, mimetype=video["content_type"], conditional=True)

    file_size = os.path.getsize(path)
    match = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not match:
        abort(416)
    start_s, end_s = match.groups()
    if not start_s and not end_s:
        abort(416)
    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    else:  # suffix range: last N bytes
        start = max(0, file_size - int(end_s))
        end = file_size - 1
    if start >= file_size or start > end:
        abort(416)
    end = min(end, file_size - 1)
    length = end - start + 1

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), status=206, mimetype=video["content_type"])
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp


@app.route("/delete/<video_id>", methods=["POST"])
@auth.login_required
def delete(video_id):
    conn = db.get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if video is None:
        abort(404)
    if video["user_id"] != g.user["id"]:
        abort(403)
    stages.delete_video_files(storage, conn, video)
    # If it's still processing, drop the staged upload too; the worker
    # notices the missing row when it finishes and discards its output.
    staged = [
        json.loads(row["input"]).get("staged_path")
        for row in conn.execute(
            "SELECT input FROM processing_jobs WHERE video_id = ? AND stage = 'ingest'"
            " AND status != 'succeeded' AND input IS NOT NULL", (video_id,))
    ]
    for name in os.listdir(app.config["INCOMING_DIR"]):
        if name.startswith(f"raw_{video_id}"):
            staged.append(os.path.join(app.config["INCOMING_DIR"], name))
    for path in staged:
        # Only files we staged ourselves (never a bulk-import source folder).
        if path and os.path.dirname(os.path.abspath(path)) == os.path.abspath(
                app.config["INCOMING_DIR"]):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    ingest.delete_video_rows(conn, video_id)
    conn.commit()
    flash("Video deleted.", "success")
    return redirect(url_for("index"))


# --- CLI: bulk import and job runner (ING-2) ----------------------------------

import click  # noqa: E402  (bundled with Flask)


def _import_defaults(competition, event_date, source, license_):
    return {"competition": competition, "event_date": event_date,
            "source": source, "license": license_}


def _import_user(username):
    conn = db.connect(app.config["DATABASE"])
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row is None:
        raise click.UsageError(f"unknown user '{username}'")
    return row["id"]


def _report(result, process):
    for relpath, video_id in result["created"]:
        click.echo(f"queued   {relpath} -> {video_id}")
    for relpath, reason in result["skipped"]:
        click.echo(f"skipped  {relpath} ({reason})")
    for relpath, reason in result["errors"]:
        click.echo(f"error    {relpath}: {reason}", err=True)
    click.echo(f"{len(result['created'])} queued, {len(result['skipped'])} skipped,"
               f" {len(result['errors'])} errors")
    if process:
        ran = jobs.run_pending(pipeline)
        click.echo(f"ran {ran} processing jobs")
    if result["errors"]:
        raise SystemExit(1)


_import_options = [
    click.option("--user", "username", required=True, help="Owner of the imported videos"),
    click.option("--license", "license_", default="", help="License for every file (required unless the manifest gives one)"),
    click.option("--competition", default="", help="Competition/event name"),
    click.option("--event-date", default="", help="Match date, YYYY-MM-DD"),
    click.option("--source", default="", help="Where the footage came from"),
    click.option("--manifest", type=click.Path(exists=True, dir_okay=False),
                 help="CSV with a 'file' column plus per-file metadata columns"),
    click.option("--process", is_flag=True,
                 help="Run the processing jobs now instead of leaving them to the worker"),
]


def _with_import_options(fn):
    for option in reversed(_import_options):
        fn = option(fn)
    return fn


@app.cli.command("import-folder")
@click.argument("folder", type=click.Path(exists=True, file_okay=False))
@click.option("--no-recursive", is_flag=True, help="Don't descend into subfolders")
@_with_import_options
def import_folder_command(folder, no_recursive, username, license_, competition,
                          event_date, source, manifest, process):
    """Bulk-import every video file under FOLDER (ING-2)."""
    conn = db.connect(app.config["DATABASE"])
    try:
        result = ingest.bulk_import_folder(
            conn, folder, user_id=_import_user(username),
            defaults=_import_defaults(competition, event_date, source, license_),
            manifest=ingest.load_manifest(manifest) if manifest else None,
            recursive=not no_recursive, max_attempts=app.config["JOB_MAX_ATTEMPTS"],
        )
    finally:
        conn.close()
    _report(result, process)


@app.cli.command("import-storage")
@click.argument("prefix")
@_with_import_options
def import_storage_command(prefix, username, license_, competition, event_date,
                           source, manifest, process):
    """Bulk-import every video already in storage under PREFIX (ING-2).

    With Azure configured this imports from the blob container (e.g. files
    dropped into `inbox/` with azcopy or Storage Explorer); locally, from the
    uploads folder.
    """
    conn = db.connect(app.config["DATABASE"])
    try:
        result = ingest.bulk_import_storage(
            conn, storage, prefix, user_id=_import_user(username),
            defaults=_import_defaults(competition, event_date, source, license_),
            manifest=ingest.load_manifest(manifest) if manifest else None,
            max_attempts=app.config["JOB_MAX_ATTEMPTS"],
        )
    except ingest.MetadataError as exc:
        raise click.UsageError(str(exc))
    finally:
        conn.close()
    _report(result, process)


@app.cli.command("run-jobs")
def run_jobs_command():
    """Run all runnable processing jobs in this process, then exit."""
    click.echo(f"ran {jobs.run_pending(pipeline)} processing jobs")


@app.template_filter("filesize")
def filesize(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024


@app.template_filter("duration")
def duration(ms):
    """Format milliseconds as H:MM:SS or M:SS."""
    seconds = int((ms or 0) // 1000)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404,
                           message="That page or video doesn't exist."), 404


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403,
                           message="You don't have permission to do that."), 403


@app.errorhandler(400)
def bad_request(e):
    message = getattr(e, "description", None) or "Bad request."
    return render_template("error.html", code=400, message=message), 400


@app.errorhandler(413)
def too_large(_e):
    flash("File is too large.", "error")
    return redirect(url_for("upload"))


if __name__ == "__main__":
    app.run(debug=True)
