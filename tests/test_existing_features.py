"""Regression tests for features that predate Phase 0."""
import io
import uuid

import db
import jobs
from conftest import make_user, login, query


def test_signup_login_logout(client):
    client.get("/signup")
    with client.session_transaction() as s:
        token = s["csrf_token"]
    resp = client.post("/signup", data={"csrf_token": token, "username": "newbie",
                                        "email": "n@example.com", "password": "password123"})
    assert resp.status_code == 302
    assert b"newbie" in client.get("/").data
    with client.session_transaction() as s:
        token = s["csrf_token"]
    client.post("/logout", data={"csrf_token": token})
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf_token"]
    resp = client.post("/login", data={"csrf_token": token, "identifier": "newbie",
                                       "password": "password123"})
    assert resp.status_code == 302


def test_post_without_csrf_rejected(logged_in):
    client, _, _ = logged_in
    assert client.post("/account/belt", data={"belt": "blue"}).status_code == 400


def test_belt_and_comments(logged_in, appmod):
    client, user_id, csrf = logged_in
    client.post("/account/belt", data={"csrf_token": csrf, "belt": "purple"})
    assert query(appmod, "SELECT belt FROM users WHERE id=?", (user_id,))[0][0] == "purple"
    vid = uuid.uuid4().hex
    conn = db.connect(appmod.app.config["DATABASE"])
    conn.execute("INSERT INTO videos (id, title, stored_name, content_type, size_bytes, user_id)"
                 " VALUES (?, 'Legacy', 'legacy.mp4', 'video/mp4', 10, ?)", (vid, user_id))
    conn.commit()
    conn.close()
    client.post(f"/watch/{vid}/comment", data={"csrf_token": csrf, "body": "Nice sweep"})
    assert b"Nice sweep" in client.get(f"/watch/{vid}").data


def test_legacy_video_still_streams_with_range(appmod, logged_in):
    """Videos stored before Phase 0 (bare key, no assets) keep working."""
    client, user_id, _ = logged_in
    appmod.storage.save(io.BytesIO(b"0123456789"), "legacy.mp4")
    vid = uuid.uuid4().hex
    conn = db.connect(appmod.app.config["DATABASE"])
    conn.execute("INSERT INTO videos (id, title, stored_name, content_type, size_bytes, user_id)"
                 " VALUES (?, 'Legacy', 'legacy.mp4', 'video/mp4', 10, ?)", (vid, user_id))
    conn.commit()
    conn.close()
    resp = client.get(f"/media/{vid}", headers={"Range": "bytes=2-5"})
    assert resp.status_code == 206 and resp.data == b"2345"
    assert resp.headers["Content-Range"] == "bytes 2-5/10"
    assert b"Legacy" in client.get("/?q=Leg").data
    # Owner deletes it: the legacy key is removed too.
    with client.session_transaction() as s:
        csrf = s["csrf_token"]
    client.post(f"/delete/{vid}", data={"csrf_token": csrf})
    assert not appmod.storage.exists("legacy.mp4")


def test_only_owner_can_delete(appmod, client):
    owner = make_user(appmod, "owner")
    other = make_user(appmod, "other")
    vid = uuid.uuid4().hex
    conn = db.connect(appmod.app.config["DATABASE"])
    conn.execute("INSERT INTO videos (id, title, stored_name, content_type, size_bytes, user_id)"
                 " VALUES (?, 'Mine', 'x.mp4', 'video/mp4', 1, ?)", (vid, owner))
    conn.commit()
    conn.close()
    csrf = login(client, other)
    assert client.post(f"/delete/{vid}", data={"csrf_token": csrf}).status_code == 403


def test_upload_requires_login(client):
    resp = client.get("/upload")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_search_matches_competitor_names(appmod, logged_in, samples):
    client, _, csrf = logged_in
    with open(samples[7], "rb") as f:
        client.post("/upload", data={"csrf_token": csrf, "video": (f, "m.mp4"),
                                     "license": "own", "participants": "Gordon Ryan (NYBJJ)"},
                    content_type="multipart/form-data")
    jobs.run_pending(appmod.pipeline)
    assert b"m</h3>" in client.get("/?q=Gordon").data
    assert b"m</h3>" not in client.get("/?q=Nobody").data
