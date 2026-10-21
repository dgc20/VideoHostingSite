"""Gate 1 (roadmap): 10 videos ingested end to end.

Ten distinct ffmpeg-generated clips enter through every Phase 0 ingestion
path — resumable upload (ING-1), the upload form, folder bulk import and
storage-prefix bulk import (ING-2), and the iCloud pipeline's /api/import —
with match metadata (ING-4). The real background worker thread processes
them; we then check every video is playable and has its immutable original,
mezzanine, 480p proxy, keyframes and thumbnail in storage (STO-1, PRE-1,
PRE-3), its metadata in the DB (STO-2), and a clean job dashboard (OPS-1).
"""
import os
import shutil

import pytest

import jobs
import processing
from conftest import FFMPEG, make_user, login, query

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


@pytest.fixture
def worker(appmod):
    w = jobs.start_worker(appmod.pipeline, poll_s=0.2)
    yield w
    jobs.stop_worker()


def resumable(client, csrf, path, chunk, **meta):
    data = open(path, "rb").read()
    body = dict(meta, filename=os.path.basename(path), size=len(data))
    session = client.post("/uploads", json=body, headers={"X-CSRF-Token": csrf}).get_json()
    offset = 0
    while offset < len(data):
        piece = data[offset:offset + chunk]
        resp = client.put(session["upload_url"], data=piece, headers={
            "X-CSRF-Token": csrf, "Content-Range":
                f"bytes {offset}-{offset + len(piece) - 1}/{len(data)}"})
        offset = resp.get_json()["offset"]
    assert resp.status_code == 201
    return resp.get_json()["video_id"]


def form_upload(client, csrf, path, **meta):
    with open(path, "rb") as f:
        resp = client.post("/upload", data=dict(meta, csrf_token=csrf,
                                                video=(f, os.path.basename(path))),
                           content_type="multipart/form-data")
    assert resp.status_code == 302
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_gate1_ten_videos_end_to_end(appmod, client, samples, tmp_path, worker):
    user_id = make_user(appmod, "coach")
    csrf = login(client, user_id)
    meta = {"license": "own-footage", "competition": "Gate 1 Open",
            "event_date": "2026-10-01"}

    # 3 x resumable upload (16 KB chunks), with competitors
    for i in range(3):
        resumable(client, csrf, samples[i], 16 * 1024,
                  participants=[{"name": f"Athlete {i}A", "team": "Alliance"},
                                {"name": f"Athlete {i}B", "team": "Atos"}], **meta)
    # 2 x upload form
    for i in range(3, 5):
        form_upload(client, csrf, samples[i], participants="A (X)\nB (Y)", **meta)
    # 3 x folder bulk import via the CLI
    folder = tmp_path / "bulk"
    folder.mkdir()
    for i in range(5, 8):
        shutil.copy(samples[i], folder / f"bulk_{i}.mp4")
    runner = appmod.app.test_cli_runner()
    result = runner.invoke(args=["import-folder", str(folder), "--user", "coach",
                                 "--license", "own-footage", "--competition", "Gate 1 Open"])
    assert "3 queued" in result.output, result.output
    # 1 x storage-prefix bulk import
    with open(samples[8], "rb") as f:
        appmod.storage.save(f, "inbox/drop.mp4")
    result = runner.invoke(args=["import-storage", "inbox/", "--user", "coach",
                                 "--license", "own-footage"])
    assert "1 queued" in result.output, result.output
    # 1 x iCloud pipeline /api/import
    with open(samples[9], "rb") as f:
        appmod.storage.save(f, "icloud123.mp4")
    resp = client.post("/api/import", headers={"Authorization": "Bearer test-token"},
                       json={"username": "coach", "stored_name": "icloud123.mp4",
                             "source_id": "icloud:123", "license": "own-footage"})
    assert resp.status_code == 201

    jobs.notify()
    assert jobs.wait_idle(appmod.pipeline, timeout=120), "pipeline did not finish"

    videos = query(appmod, "SELECT * FROM videos")
    assert len(videos) == 10
    assert len({v["content_hash"] for v in videos}) == 10
    for v in videos:
        vid = v["id"]
        assert v["status"] == "ready", (v["title"], v["status"])
        assert v["license"] == "own-footage"
        assert v["duration_ms"] and v["fps"] and v["width"] and v["height"]
        stages_ = dict(query(appmod, "SELECT stage, status FROM processing_jobs"
                                     " WHERE video_id=?", (vid,)))
        assert stages_ == dict.fromkeys(jobs.STAGES, "succeeded"), (v["title"], stages_)
        assets = query(appmod, "SELECT kind, uri, height FROM assets WHERE video_id=?", (vid,))
        kinds = [a["kind"] for a in assets]
        for kind in ("original", "mezzanine", "proxy", "thumbnail"):
            assert kinds.count(kind) == 1, (v["title"], kind)
        assert kinds.count("keyframe") >= 1
        for a in assets:
            assert appmod.storage.exists(a["uri"]), a["uri"]
        heights = {a["kind"]: a["height"] for a in assets}
        assert heights["mezzanine"] <= 1080 and heights["proxy"] <= 480
        mezz = next(a["uri"] for a in assets if a["kind"] == "mezzanine")
        assert processing.probe(FFMPEG, appmod.storage.path(mezz))["codec"] == "h264"
        # Playable: the watch page has a player and the media streams with Range.
        assert b"<video" in client.get(f"/watch/{vid}").data
        media = client.get(f"/media/{vid}", headers={"Range": "bytes=0-99"})
        assert media.status_code == 206 and len(media.data) == 100

    competitions = {v["competition"] for v in videos}
    assert competitions == {"Gate 1 Open", ""}  # storage/API imports had none
    assert query(appmod, "SELECT COUNT(*) FROM video_participants")[0][0] == 10

    # Job dashboard: all ten done, every stage succeeded ten times.
    admin = make_user(appmod, "admin")
    login(client, admin)
    board = client.get("/jobs?format=json").get_json()
    assert len(board["videos"]) == 10
    assert {v["state"] for v in board["videos"]} == {"done"}
    for stage in jobs.STAGES:
        assert board["totals"][stage]["succeeded"] == 10
    # No staging or work files left behind.
    incoming = appmod.app.config["INCOMING_DIR"]
    assert not [n for n in os.listdir(incoming) if n.startswith(("upload_", "resumable_"))]
    assert os.listdir(appmod.pipeline.work_root) == []
