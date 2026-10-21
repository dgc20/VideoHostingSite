"""Shared fixtures: an isolated app per test and ffmpeg-generated samples.

Everything runs offline: storage is the local-disk backend in a temp dir,
the background job worker is disabled (tests drain the queue explicitly with
jobs.run_pending, except the Gate 1 test which exercises the real worker),
and sample videos are synthesised with ffmpeg's lavfi test sources.
"""
import importlib
import subprocess
import sys
import uuid

import pytest

import processing


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    """A freshly configured `app` module backed by a temp DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOB_WORKER", "0")
    monkeypatch.setenv("IMPORT_API_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_USERNAMES", "admin")
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    if "app" in sys.modules:
        mod = importlib.reload(sys.modules["app"])
    else:
        mod = importlib.import_module("app")
    mod.app.config["TESTING"] = True
    yield mod


@pytest.fixture
def client(appmod):
    return appmod.app.test_client()


def make_user(appmod, username="alice"):
    """Insert a user row directly and return its id."""
    import auth
    import db

    user_id = uuid.uuid4().hex
    conn = db.connect(appmod.app.config["DATABASE"])
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, ?)",
        (user_id, username, f"{username}@example.com", auth.hash_password("password123")),
    )
    conn.commit()
    conn.close()
    return user_id


def login(client, user_id, csrf="test-csrf"):
    """Log the test client in as user_id with a known CSRF token."""
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["csrf_token"] = csrf
    return csrf


@pytest.fixture
def logged_in(appmod, client):
    """(client, user_id, csrf) for a logged-in user 'alice'."""
    user_id = make_user(appmod, "alice")
    csrf = login(client, user_id)
    return client, user_id, csrf


def query(appmod, sql, params=()):
    import db

    conn = db.connect(appmod.app.config["DATABASE"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- sample footage -------------------------------------------------------

FFMPEG = processing.find_ffmpeg()


def make_video(path, seconds=3, freq=440, size="320x240", pattern="testsrc",
               audio=True):
    """Synthesise a short H.264/AAC MP4 with ffmpeg's lavfi sources.

    Different `freq` values give different bytes (and content hashes).
    """
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=15:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else []
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return str(path)


def make_scene_video(path, size="320x240"):
    """A 6s clip with hard cuts at 2s and 4s (red → blue → green)."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for color in ("red", "blue", "green"):
        cmd += ["-f", "lavfi", "-i", f"color=c={color}:size={size}:rate=15:duration=2"]
    cmd += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return str(path)


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """Ten distinct ~2-3 s sample clips (Gate 1 footage)."""
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    root = tmp_path_factory.mktemp("samples")
    paths = []
    for i in range(10):
        pattern = "testsrc" if i % 2 == 0 else "testsrc2"
        paths.append(make_video(root / f"match_{i:02d}.mp4", seconds=2 + i % 2,
                                freq=300 + 50 * i, pattern=pattern))
    return paths


@pytest.fixture(scope="session")
def scene_clip(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    return make_scene_video(tmp_path_factory.mktemp("scene") / "cuts.mp4")
