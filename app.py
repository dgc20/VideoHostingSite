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
import uuid

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

import auth
import db
import storage as storage_module

ALLOWED_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v"}

# Azure App Service persists /home across restarts; use it when available so
# the SQLite database and local uploads survive redeploys.
_default_data_dir = "/home/data" if os.path.isdir("/home") and os.access("/home", os.W_OK) else "instance"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024,
    DATA_DIR=os.environ.get("DATA_DIR", _default_data_dir),
    AZURE_STORAGE_CONNECTION_STRING=os.environ.get("AZURE_STORAGE_CONNECTION_STRING"),
    AZURE_CONTAINER_NAME=os.environ.get("AZURE_CONTAINER_NAME", "videos"),
    # Session cookie hardening. SECURE defaults off so local http works;
    # set SESSION_COOKIE_SECURE=1 in production (Azure serves over HTTPS).
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
)
os.makedirs(app.config["DATA_DIR"], exist_ok=True)
app.config["DATABASE"] = os.path.join(app.config["DATA_DIR"], "videos.db")
app.config["UPLOAD_DIR"] = os.path.join(app.config["DATA_DIR"], "uploads")

db.init_db(app)
storage = storage_module.create_storage(app.config)

# Load the logged-in user and enforce CSRF on every request.
app.before_request(auth.load_logged_in_user)
app.before_request(auth.check_csrf)


@app.context_processor
def inject_globals():
    return {"current_user": g.get("user"), "csrf_token": auth.get_csrf_token}


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


@app.route("/account")
@auth.login_required
def account():
    videos = db.get_db().execute(
        "SELECT * FROM videos WHERE user_id = ? ORDER BY uploaded_at DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template("account.html", videos=videos)


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    conn = db.get_db()
    base = (
        "SELECT v.*, u.username AS uploader FROM videos v"
        " LEFT JOIN users u ON u.id = v.user_id"
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

        if storage.is_remote:
            size = storage.save(file.stream, stored_name, content_type)
        else:
            size = storage.save(file.stream, stored_name)

        conn = db.get_db()
        conn.execute(
            "INSERT INTO videos (id, title, description, stored_name,"
            " content_type, size_bytes, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (video_id, title, description, stored_name, content_type, size,
             g.user["id"]),
        )
        conn.commit()
        flash("Video uploaded.", "success")
        return redirect(url_for("watch", video_id=video_id))

    return render_template("upload.html")


@app.route("/watch/<video_id>")
def watch(video_id):
    conn = db.get_db()
    video = conn.execute(
        "SELECT v.*, u.username AS uploader FROM videos v"
        " LEFT JOIN users u ON u.id = v.user_id WHERE v.id = ?",
        (video_id,),
    ).fetchone()
    if video is None:
        abort(404)
    conn.execute("UPDATE videos SET views = views + 1 WHERE id = ?", (video_id,))
    conn.commit()

    if storage.is_remote:
        src = storage.playback_url(video["stored_name"])
    else:
        src = url_for("media", video_id=video_id)
    is_owner = g.user is not None and g.user["id"] == video["user_id"]
    comments = conn.execute(
        "SELECT c.id, c.body, c.created_at, c.user_id, u.username"
        " FROM comments c JOIN users u ON u.id = c.user_id"
        " WHERE c.video_id = ? ORDER BY c.created_at DESC, c.rowid DESC",
        (video_id,),
    ).fetchall()
    return render_template(
        "watch.html", video=video, src=src, is_owner=is_owner, comments=comments
    )


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
    if video is None:
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
