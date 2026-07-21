"""A simple video hosting website.

Run locally:
    pip install -r requirements.txt
    flask --app app run --debug

In production (Azure App Service):
    gunicorn --bind=0.0.0.0:8000 --timeout 600 app:app
"""
import mimetypes
import os
import re
import secrets
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
import processing
import storage as storage_module

ALLOWED_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v"}
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
)
os.makedirs(app.config["DATA_DIR"], exist_ok=True)
app.config["DATABASE"] = os.path.join(app.config["DATA_DIR"], "videos.db")
app.config["UPLOAD_DIR"] = os.path.join(app.config["DATA_DIR"], "uploads")
# Uploads land here first; large ones stay while compression runs.
app.config["INCOMING_DIR"] = os.path.join(app.config["DATA_DIR"], "incoming")
os.makedirs(app.config["INCOMING_DIR"], exist_ok=True)

db.init_db(app)
storage = storage_module.create_storage(app.config)

# Restart any compressions that were interrupted by a redeploy/restart.
processing.resume_pending(
    app.config["DATABASE"], storage, app.config["INCOMING_DIR"]
)

# Load the logged-in user and enforce CSRF on every request.
app.before_request(auth.load_logged_in_user)
app.before_request(auth.check_csrf)


@app.context_processor
def inject_globals():
    return {
        "current_user": g.get("user"),
        "csrf_token": auth.get_csrf_token,
        "BELTS": BELTS,
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
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    thumbnail_name = (data.get("thumbnail_name") or "").strip() or None

    if not username or not stored_name or not source_id:
        return jsonify(error="username, stored_name and source_id are required"), 400
    # Stored names must be bare blob names — never paths.
    for value in (stored_name, thumbnail_name):
        if value and ("/" in value or "\\" in value or ".." in value):
            return jsonify(error="invalid stored_name"), 400

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

    if not title:
        title = os.path.splitext(stored_name)[0]
    content_type = (
        mimetypes.guess_type(stored_name)[0]
        or data.get("content_type")
        or "application/octet-stream"
    )
    try:
        size = int(data.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0

    video_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO videos (id, title, description, stored_name,"
            " content_type, size_bytes, user_id, status, source_id,"
            " thumbnail_name) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)",
            (video_id, title, description, stored_name, content_type, size,
             user["id"], source_id, thumbnail_name),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Concurrent import of the same source_id won the race.
        row = conn.execute(
            "SELECT id FROM videos WHERE source_id = ?", (source_id,)
        ).fetchone()
        return jsonify(id=row["id"], status="exists"), 200
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
    if q:
        videos = conn.execute(
            base + " WHERE v.title LIKE ? OR v.description LIKE ?"
            " ORDER BY v.uploaded_at DESC",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        videos = conn.execute(base + " ORDER BY v.uploaded_at DESC").fetchall()
    return render_template("index.html", videos=videos, q=q)


@app.route("/upload", methods=["GET", "POST"])
@auth.login_required
def upload():
    if request.method == "POST":
        file = request.files.get("video")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if not file or not file.filename:
            flash("Please choose a video file.", "error")
            return redirect(url_for("upload"))
        if not _allowed(file.filename):
            flash(
                "Unsupported file type. Allowed: "
                + ", ".join(sorted(ALLOWED_EXTENSIONS)),
                "error",
            )
            return redirect(url_for("upload"))
        if not title:
            title = os.path.splitext(secure_filename(file.filename))[0] or "Untitled"

        ext = os.path.splitext(file.filename)[1].lower()
        video_id = uuid.uuid4().hex
        stored_name = f"{video_id}{ext}"
        content_type = (
            mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
        )

        # Land the upload on disk first so we can check its size before
        # deciding whether it needs background compression.
        raw_path = os.path.join(
            app.config["INCOMING_DIR"], f"raw_{video_id}{ext}"
        )
        file.save(raw_path)
        size = os.path.getsize(raw_path)

        conn = db.get_db()
        if size <= app.config["COMPRESS_THRESHOLD"]:
            try:
                with open(raw_path, "rb") as f:
                    if storage.is_remote:
                        storage.save(f, stored_name, content_type)
                    else:
                        storage.save(f, stored_name)
                # Extract a poster frame while we still have the local file.
                thumbnail_name = processing.store_thumbnail(
                    storage, video_id, raw_path
                )
            finally:
                os.remove(raw_path)
            conn.execute(
                "INSERT INTO videos (id, title, description, stored_name,"
                " content_type, size_bytes, user_id, status, thumbnail_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
                (video_id, title, description, stored_name, content_type,
                 size, g.user["id"], thumbnail_name),
            )
            conn.commit()
            flash("Video uploaded.", "success")
        else:
            conn.execute(
                "INSERT INTO videos (id, title, description, stored_name,"
                " content_type, size_bytes, user_id, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'processing')",
                (video_id, title, description, stored_name, content_type,
                 size, g.user["id"]),
            )
            conn.commit()
            processing.process_async(
                app.config["DATABASE"], storage, video_id, raw_path
            )
            flash(
                "Video uploaded — it's being compressed and will be ready"
                " to watch in a few minutes.",
                "success",
            )
        return redirect(url_for("watch", video_id=video_id))

    return render_template("upload.html")


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
    poster = url_for("thumb", video_id=video_id) if video["thumbnail_name"] else None
    comments = conn.execute(
        "SELECT c.id, c.body, c.created_at, c.user_id, u.username, u.belt"
        " FROM comments c JOIN users u ON u.id = c.user_id"
        " WHERE c.video_id = ? ORDER BY c.created_at DESC, c.rowid DESC",
        (video_id,),
    ).fetchall()
    return render_template(
        "watch.html", video=video, src=src, poster=poster,
        is_owner=is_owner, comments=comments
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
    storage.delete(video["stored_name"])
    if video["thumbnail_name"]:
        storage.delete(video["thumbnail_name"])
    # If it's still compressing, drop the raw upload too; the worker notices
    # the missing row when it finishes and discards its output.
    for name in os.listdir(app.config["INCOMING_DIR"]):
        if name.startswith(f"raw_{video_id}"):
            try:
                os.remove(os.path.join(app.config["INCOMING_DIR"], name))
            except FileNotFoundError:
                pass
    conn.execute("DELETE FROM comments WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    flash("Video deleted.", "success")
    return redirect(url_for("index"))


@app.template_filter("filesize")
def filesize(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024


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
