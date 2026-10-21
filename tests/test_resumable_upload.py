"""ING-1: resumable direct upload, up to 20 GB per file."""
import os

import jobs
from conftest import make_user, login, query

META = {"license": "own-footage", "competition": "ADCC Trials",
        "participants": [{"name": "Ana", "team": "Alliance"}]}


def start(client, csrf, filename, size, **extra):
    body = dict(META, filename=filename, size=size, **extra)
    return client.post("/uploads", json=body, headers={"X-CSRF-Token": csrf})


def put(client, csrf, url, data, start_at, total):
    return client.put(url, data=data, headers={
        "X-CSRF-Token": csrf, "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes {start_at}-{start_at + len(data) - 1}/{total}"})


def test_chunked_upload_with_interruption_and_resume(appmod, logged_in, samples):
    client, _, csrf = logged_in
    appmod.app.config["UPLOAD_CHUNK_BYTES"] = 16 * 1024
    data = open(samples[0], "rb").read()
    resp = start(client, csrf, "final.mp4", len(data), title="Resumed final")
    assert resp.status_code == 201
    session = resp.get_json()
    assert session["offset"] == 0 and session["chunk_size"] == 16 * 1024
    url = session["upload_url"]

    # First chunk lands.
    chunk = 16 * 1024
    assert put(client, csrf, url, data[:chunk], 0, len(data)).get_json()["offset"] == chunk
    # The connection drops mid-chunk: a truncated body doesn't advance the offset.
    short = client.put(url, data=data[chunk:chunk + 100], headers={
        "X-CSRF-Token": csrf, "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes {chunk}-{2 * chunk - 1}/{len(data)}"})
    assert short.status_code == 400 and short.get_json()["offset"] == chunk
    # A client that lost track sends from the wrong offset and is told where to resume.
    wrong = put(client, csrf, url, data[0:chunk], 0, len(data))
    assert wrong.status_code == 409 and wrong.get_json()["offset"] == chunk
    # The client asks for the offset (e.g. after a page reload) and resumes.
    offset = client.get(url).get_json()["offset"]
    while offset < len(data):
        resp = put(client, csrf, url, data[offset:offset + chunk], offset, len(data))
        offset = resp.get_json()["offset"]
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "complete" and body["watch_url"].endswith(body["video_id"])

    jobs.run_pending(appmod.pipeline)
    video = query(appmod, "SELECT * FROM videos WHERE id=?", (body["video_id"],))[0]
    assert video["status"] == "ready" and video["title"] == "Resumed final"
    assert video["competition"] == "ADCC Trials"
    key = query(appmod, "SELECT uri FROM assets WHERE video_id=? AND kind='original'",
                (video["id"],))[0][0]
    assert open(appmod.storage.path(key), "rb").read() == data  # byte-identical
    assert not [n for n in os.listdir(appmod.app.config["INCOMING_DIR"])
                if n.startswith(("resumable_", "upload_"))]


def test_twenty_gb_limit(logged_in):
    client, _, csrf = logged_in
    twenty_gb = 20 * 1024 ** 3
    assert start(client, csrf, "big.mp4", twenty_gb + 1).status_code == 413
    # Exactly 20 GB is accepted when the disk has room (checked separately).
    import shutil
    from unittest import mock
    with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=twenty_gb * 2)):
        assert start(client, csrf, "big.mp4", twenty_gb).status_code == 201


def test_not_enough_disk_is_refused(logged_in):
    client, _, csrf = logged_in
    import shutil
    from unittest import mock
    with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=10)):
        assert start(client, csrf, "a.mp4", 1000).status_code == 507


def test_validation(logged_in):
    client, _, csrf = logged_in
    assert start(client, csrf, "a.exe", 10).status_code == 400
    assert start(client, csrf, "a.mp4", 0).status_code == 400
    resp = client.post("/uploads", json={"filename": "a.mp4", "size": 10},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400 and "license" in resp.get_json()["error"]
    url = start(client, csrf, "a.mp4", 10).get_json()["upload_url"]
    bad = client.put(url, data=b"x" * 10, headers={"X-CSRF-Token": csrf,
                                                   "Content-Range": "bytes 0-9/99"})
    assert bad.status_code == 400
    assert client.put(url, data=b"x", headers={"X-CSRF-Token": csrf}).status_code == 400


def test_chunk_size_cap(appmod, logged_in):
    client, _, csrf = logged_in
    appmod.app.config["UPLOAD_CHUNK_BYTES"] = 10
    url = start(client, csrf, "a.mp4", 100).get_json()["upload_url"]
    assert put(client, csrf, url, b"x" * 11, 0, 100).status_code == 413


def test_requires_login_and_csrf(appmod, client):
    assert client.post("/uploads", json=dict(META, filename="a.mp4", size=1)).status_code in (400, 401)
    user = make_user(appmod, "bob")
    login(client, user)
    no_csrf = client.post("/uploads", json=dict(META, filename="a.mp4", size=1))
    assert no_csrf.status_code == 400


def test_sessions_are_private_and_abortable(appmod, logged_in, client):
    client, _, csrf = logged_in
    url = start(client, csrf, "a.mp4", 100).get_json()["upload_url"]
    upload_id = url.rsplit("/", 1)[-1]
    staged = os.path.join(appmod.app.config["INCOMING_DIR"], f"resumable_{upload_id}.part")
    assert os.path.exists(staged)
    other_client = appmod.app.test_client()
    login(other_client, make_user(appmod, "eve"))
    assert other_client.get(url).status_code == 404
    resp = client.delete(url, headers={"X-CSRF-Token": csrf})
    assert resp.get_json()["status"] == "aborted" and not os.path.exists(staged)
    assert put(client, csrf, url, b"x", 0, 100).status_code == 409


def test_stale_sessions_expire(appmod, logged_in):
    client, _, csrf = logged_in
    url = start(client, csrf, "a.mp4", 100).get_json()["upload_url"]
    import db
    conn = db.connect(appmod.app.config["DATABASE"])
    conn.execute("UPDATE upload_sessions SET updated_at = '2000-01-01 00:00:00'")
    conn.commit()
    conn.close()
    start(client, csrf, "b.mp4", 100)  # creating a session sweeps stale ones
    assert client.get(url).get_json()["status"] == "aborted"


def test_upload_page_includes_resumable_client(logged_in):
    client, _, _ = logged_in
    page = client.get("/upload").data
    assert b"upload.js" in page and b'data-max-bytes="21474836480"' in page
    assert b'value="own-footage"' in page
