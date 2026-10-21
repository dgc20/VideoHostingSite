"""OPS-1: job dashboard with per-video stage status, retries and failures."""
import pytest

import db
import jobs
import stages
from conftest import login, make_user, query
from test_pipeline import upload


@pytest.fixture
def two_users(appmod, client):
    alice = make_user(appmod, "alice")
    bob = make_user(appmod, "bob")
    return alice, bob


def test_requires_login(client):
    assert client.get("/jobs").status_code == 302


def test_shows_stage_status_attempts_and_errors(appmod, client, two_users, samples,
                                                monkeypatch):
    alice, _ = two_users
    csrf = login(client, alice)
    good = upload(client, csrf, samples[0], title="Good match")
    bad = upload(client, csrf, samples[1], title="Bad match")
    real = stages.STAGE_FUNCTIONS["transcode"]

    def flaky(ctx, conn, video, job_input):
        if video["id"] == bad:
            raise RuntimeError("encoder exploded")
        return real(ctx, conn, video, job_input)

    monkeypatch.setitem(stages.STAGE_FUNCTIONS, "transcode", flaky)
    jobs.run_pending(appmod.pipeline)

    page = client.get("/jobs").data.decode()
    assert "Good match" in page and "Bad match" in page
    assert "encoder exploded" in page and "try 3/3" in page
    assert page.count("Retry") >= 1
    data = client.get("/jobs?format=json").get_json()
    states = {v["title"]: v["state"] for v in data["videos"]}
    assert states == {"Good match": "done", "Bad match": "failed"}
    assert data["totals"]["transcode"]["failed"] == 1
    assert data["totals"]["keyframes"]["skipped"] == 1
    bad_stages = next(v for v in data["videos"] if v["title"] == "Bad match")["stages"]
    assert [s["status"] for s in bad_stages] == ["succeeded", "succeeded", "failed", "skipped"]

    failed_only = client.get("/jobs?state=failed&format=json").get_json()["videos"]
    assert [v["title"] for v in failed_only] == ["Bad match"]
    one = client.get(f"/jobs?video={good}&format=json").get_json()["videos"]
    assert [v["video_id"] for v in one] == [good]

    # Fix the cause and retry from the dashboard: the video completes.
    monkeypatch.setitem(stages.STAGE_FUNCTIONS, "transcode", real)
    job_id = query(appmod, "SELECT id FROM processing_jobs WHERE video_id=? AND stage='transcode'",
                   (bad,))[0][0]
    resp = client.post(f"/jobs/{job_id}/retry", data={"csrf_token": csrf, "next": "/jobs?state=failed"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/jobs?state=failed")
    jobs.run_pending(appmod.pipeline)
    assert {v["state"] for v in client.get("/jobs?format=json").get_json()["videos"]} == {"done"}


def test_users_see_only_their_videos_admin_sees_all(appmod, client, two_users, samples):
    alice, bob = two_users
    upload(client, login(client, alice), samples[2], title="Alice video")
    upload(client, login(client, bob), samples[3], title="Bob video")
    login(client, bob)
    assert [v["title"] for v in client.get("/jobs?format=json").get_json()["videos"]] == ["Bob video"]
    admin = make_user(appmod, "admin")  # listed in ADMIN_USERNAMES by conftest
    login(client, admin)
    titles = {v["title"] for v in client.get("/jobs?format=json").get_json()["videos"]}
    assert titles == {"Alice video", "Bob video"}


def test_retry_permissions(appmod, client, two_users, samples):
    alice, bob = two_users
    vid = upload(client, login(client, alice), samples[4])
    conn = db.connect(appmod.pipeline.database)
    conn.execute("UPDATE processing_jobs SET status='failed' WHERE video_id=? AND stage='ingest'", (vid,))
    conn.commit()
    job_id = conn.execute("SELECT id FROM processing_jobs WHERE video_id=? AND stage='ingest'", (vid,)).fetchone()[0]
    conn.close()
    csrf = login(client, bob)
    assert client.post(f"/jobs/{job_id}/retry", data={"csrf_token": csrf}).status_code == 403
    assert client.post("/jobs/nope/retry", data={"csrf_token": csrf}).status_code == 404
    csrf = login(client, make_user(appmod, "admin"))
    assert client.post(f"/jobs/{job_id}/retry", data={"csrf_token": csrf}).status_code == 302
    assert query(appmod, "SELECT status FROM processing_jobs WHERE id=?", (job_id,))[0][0] == "queued"


def test_active_jobs_auto_refresh(appmod, logged_in, samples):
    client, _, csrf = logged_in
    upload(client, csrf, samples[5])
    page = client.get("/jobs?state=active").data
    assert b'http-equiv="refresh"' in page and b"queued" in page
    jobs.run_pending(appmod.pipeline)
    assert b'http-equiv="refresh"' not in client.get("/jobs").data
