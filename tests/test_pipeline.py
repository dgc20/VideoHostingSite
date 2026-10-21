"""Upload form → ingest → probe → transcode → keyframes, against real ffmpeg
and the local storage backend (PRE-1, PRE-3, STO-1, ING-5 exact dedupe)."""
import os

import pytest

import jobs
import processing
from conftest import FFMPEG, query

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


def upload(client, csrf, path, **fields):
    data = {"csrf_token": csrf, "license": "own-footage"}
    data.update(fields)
    with open(path, "rb") as f:
        data["video"] = (f, os.path.basename(path))
        resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302, resp.data
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_form_upload_runs_full_pipeline(appmod, logged_in, samples):
    client, _, csrf = logged_in
    vid = upload(client, csrf, samples[0], title="Final", competition="Worlds",
                 event_date="2026-06-01", participants="Ana (Alliance)\nBea (Checkmat)")
    assert query(appmod, "SELECT status FROM videos WHERE id=?", (vid,))[0][0] == "processing"
    jobs.run_pending(appmod.pipeline)

    video = query(appmod, "SELECT * FROM videos WHERE id=?", (vid,))[0]
    assert video["status"] == "ready"
    assert video["competition"] == "Worlds" and video["license"] == "own-footage"
    assert video["duration_ms"] > 1500 and video["fps"] == 15
    assert len(video["content_hash"]) == 64
    assert video["stored_name"] == f"videos/{vid}/mezzanine-v1.mp4"
    assert video["thumbnail_name"] == f"videos/{vid}/thumbnail-v1.jpg"

    kinds = [r[0] for r in query(appmod, "SELECT kind FROM assets WHERE video_id=?", (vid,))]
    assert kinds.count("original") == kinds.count("mezzanine") == kinds.count("proxy") == 1
    assert kinds.count("keyframe") >= 1 and "thumbnail" in kinds
    for (uri,) in query(appmod, "SELECT uri FROM assets WHERE video_id=?", (vid,)):
        assert appmod.storage.exists(uri), uri
    proxy = query(appmod, "SELECT uri FROM assets WHERE video_id=? AND kind='proxy'", (vid,))[0][0]
    assert processing.probe(FFMPEG, appmod.storage.path(proxy))["height"] == 240  # no upscale
    # The staged upload is gone and the work dir is cleaned up.
    assert not [n for n in os.listdir(appmod.app.config["INCOMING_DIR"]) if n.startswith("upload_")]
    assert not os.listdir(appmod.pipeline.work_root)

    page = client.get(f"/watch/{vid}")
    assert b"Worlds" in page.data and b"Ana (Alliance) vs" in page.data
    assert client.get(f"/media/{vid}").status_code == 200
    assert client.get(f"/thumb/{vid}").status_code == 200


def test_original_is_byte_identical_and_immutable(appmod, logged_in, samples):
    client, _, csrf = logged_in
    vid = upload(client, csrf, samples[1])
    jobs.run_pending(appmod.pipeline)
    key = query(appmod, "SELECT uri FROM assets WHERE video_id=? AND kind='original'", (vid,))[0][0]
    with open(samples[1], "rb") as a, open(appmod.storage.path(key), "rb") as b:
        assert a.read() == b.read()
    import io
    with pytest.raises(FileExistsError):
        appmod.storage.save(io.BytesIO(b"x"), key, overwrite=False)


def test_exact_duplicate_is_flagged_not_reprocessed(appmod, logged_in, samples):
    client, _, csrf = logged_in
    first = upload(client, csrf, samples[2])
    second = upload(client, csrf, samples[2], title="Same file again")
    jobs.run_pending(appmod.pipeline)
    row = query(appmod, "SELECT status, duplicate_of FROM videos WHERE id=?", (second,))[0]
    assert tuple(row) == ("duplicate", first)
    assert query(appmod, "SELECT COUNT(*) FROM assets WHERE video_id=?", (second,))[0][0] == 0
    stages_ = dict(query(appmod, "SELECT stage, status FROM processing_jobs WHERE video_id=?", (second,)))
    assert stages_ == {"ingest": "succeeded", "probe": "skipped",
                       "transcode": "skipped", "keyframes": "skipped"}
    assert b"Already in the library" in client.get(f"/watch/{second}").data
    assert b"Same file again" not in client.get("/").data


def test_undecodable_upload_keeps_original_playable(appmod, logged_in, tmp_path):
    """As before the pipeline: if ffmpeg can't read it, serve the original."""
    client, _, csrf = logged_in
    junk = tmp_path / "broken.mp4"
    junk.write_bytes(b"\x00" * 2048)
    vid = upload(client, csrf, str(junk))
    jobs.run_pending(appmod.pipeline)
    video = query(appmod, "SELECT status, stored_name FROM videos WHERE id=?", (vid,))[0]
    assert video["status"] == "ready"
    assert video["stored_name"] == f"videos/{vid}/original.mp4"
    s = dict(query(appmod, "SELECT stage, status FROM processing_jobs WHERE video_id=?", (vid,)))
    assert (s["probe"], s["transcode"]) == ("failed", "skipped")


def test_ffmpeg_missing_skips_processing(appmod, logged_in, samples, monkeypatch):
    client, _, csrf = logged_in
    monkeypatch.setattr(processing, "find_ffmpeg", lambda: None)
    vid = upload(client, csrf, samples[3])
    jobs.run_pending(appmod.pipeline)
    s = dict(query(appmod, "SELECT stage, status FROM processing_jobs WHERE video_id=?", (vid,)))
    assert s == {"ingest": "succeeded", "probe": "skipped",
                 "transcode": "skipped", "keyframes": "skipped"}
    assert query(appmod, "SELECT status FROM videos WHERE id=?", (vid,))[0][0] == "ready"


def test_retried_stage_adopts_existing_objects(appmod, logged_in, samples):
    """A stage that stored its outputs but failed before recording them can
    be re-run without overwriting anything (write-once storage)."""
    client, _, csrf = logged_in
    vid = upload(client, csrf, samples[4])
    jobs.run_pending(appmod.pipeline)
    import db
    conn = db.connect(appmod.pipeline.database)
    conn.execute("DELETE FROM assets WHERE video_id=? AND kind IN ('mezzanine','proxy')", (vid,))
    conn.execute("UPDATE processing_jobs SET status='failed' WHERE video_id=? AND stage='transcode'", (vid,))
    job_id = conn.execute("SELECT id FROM processing_jobs WHERE video_id=? AND stage='transcode'", (vid,)).fetchone()[0]
    jobs.retry(conn, job_id)
    conn.commit()
    conn.close()
    jobs.run_pending(appmod.pipeline)
    s = dict(query(appmod, "SELECT stage, status FROM processing_jobs WHERE video_id=?", (vid,)))
    assert s["transcode"] == "succeeded"
    assert query(appmod, "SELECT COUNT(*) FROM assets WHERE video_id=? AND kind='mezzanine'", (vid,))[0][0] == 1


def test_delete_removes_all_objects(appmod, logged_in, samples):
    client, _, csrf = logged_in
    vid = upload(client, csrf, samples[5])
    jobs.run_pending(appmod.pipeline)
    assert appmod.storage.list(f"videos/{vid}/")
    resp = client.post(f"/delete/{vid}", data={"csrf_token": csrf})
    assert resp.status_code == 302
    assert appmod.storage.list(f"videos/{vid}/") == []
    for table in ("videos", "assets", "processing_jobs", "video_participants"):
        col = "id" if table == "videos" else "video_id"
        assert query(appmod, f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (vid,))[0][0] == 0


def test_delete_while_queued_removes_staged_upload(appmod, logged_in, samples):
    client, _, csrf = logged_in
    vid = upload(client, csrf, samples[6])
    incoming = appmod.app.config["INCOMING_DIR"]
    assert [n for n in os.listdir(incoming) if n.startswith("upload_")]
    client.post(f"/delete/{vid}", data={"csrf_token": csrf})
    assert not [n for n in os.listdir(incoming) if n.startswith("upload_")]
    assert jobs.run_pending(appmod.pipeline) == 0
