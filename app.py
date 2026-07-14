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
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

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
)
os.makedirs(app.config["DATA_DIR"], exist_ok=True)
app.config["DATABASE"] = os.path.join(app.config["DATA_DIR"], "videos.db")
app.config["UPLOAD_DIR"] = os.path.join(app.config["DATA_DIR"], "uploads")

db.init_db(app)
storage = storage_module.create_storage(app.config)


def _allowed(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    conn = db.get_db()
    if q:
        videos = conn.execute(
            "SELECT * FROM videos WHERE title LIKE ? OR description LIKE ?"
            " ORDER BY uploaded_at DESC",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        videos = conn.execute(
            "SELECT * FROM videos ORDER BY uploaded_at DESC"
        ).fetchall()
    return render_template("index.html", videos=videos, q=q)


@app.route("/upload", methods=["GET", "POST"])
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
            " content_type, size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, title, description, stored_name, content_type, size),
        )
        conn.commit()
        flash("Video uploaded.", "success")
        return redirect(url_for("watch", video_id=video_id))

    return render_template("upload.html")


@app.route("/watch/<video_id>")
def watch(video_id):
    conn = db.get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if video is None:
        abort(404)
    conn.execute("UPDATE videos SET views = views + 1 WHERE id = ?", (video_id,))
    conn.commit()

    if storage.is_remote:
        src = storage.playback_url(video["stored_name"])
    else:
        src = url_for("media", video_id=video_id)
    return render_template("watch.html", video=video, src=src)


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
def delete(video_id):
    conn = db.get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if video is None:
        abort(404)
    storage.delete(video["stored_name"])
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
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_e):
    flash("File is too large.", "error")
    return redirect(url_for("upload"))


if __name__ == "__main__":
    app.run(debug=True)
