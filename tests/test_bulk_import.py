"""ING-2: bulk import from a folder or a storage prefix (the repo's
"bucket"), plus the iCloud pipeline's /api/import, all feeding the same
processing pipeline."""
import io
import os
import shutil

import jobs
from conftest import make_user, query


def write_manifest(path, rows):
    import csv
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "title", "participants", "license"])
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_folder_import_with_manifest_is_idempotent(appmod, tmp_path, samples):
    make_user(appmod, "coach")
    folder = tmp_path / "footage"
    (folder / "day2").mkdir(parents=True)
    shutil.copy(samples[0], folder / "r1.mp4")
    shutil.copy(samples[1], folder / "day2" / "final.mov")
    (folder / "notes.txt").write_text("not a video")
    manifest = write_manifest(tmp_path / "m.csv", [
        {"file": "day2/final.mov", "title": "The final",
         "participants": "Ana (Alliance); Bea (Checkmat)", "license": "event-licensed"}])
    runner = appmod.app.test_cli_runner()
    result = runner.invoke(args=[
        "import-folder", str(folder), "--user", "coach", "--license", "own-footage",
        "--competition", "Club Open", "--event-date", "2026-05-02",
        "--manifest", manifest, "--process"])
    assert result.exit_code == 0, result.output
    assert "2 queued, 0 skipped, 0 errors" in result.output
    rows = {r["title"]: r for r in query(appmod, "SELECT * FROM videos")}
    assert set(rows) == {"r1", "The final"}
    final = rows["The final"]
    assert (final["license"], final["competition"], final["event_date"]) == (
        "event-licensed", "Club Open", "2026-05-02")
    assert final["status"] == "ready" and final["source_id"].startswith("folder:")
    names = [r[0] for r in query(appmod, "SELECT name FROM video_participants"
                                         " WHERE video_id=? ORDER BY position", (final["id"],))]
    assert names == ["Ana", "Bea"]
    assert all(s == "succeeded" for (s,) in query(appmod, "SELECT status FROM processing_jobs"))
    # Source files are left untouched.
    assert (folder / "r1.mp4").exists() and (folder / "day2" / "final.mov").exists()

    again = runner.invoke(args=["import-folder", str(folder), "--user", "coach",
                                "--license", "own-footage"])
    assert "0 queued, 2 skipped" in again.output
    assert query(appmod, "SELECT COUNT(*) FROM videos")[0][0] == 2


def test_folder_import_refuses_files_without_license(appmod, tmp_path, samples):
    make_user(appmod, "coach")
    folder = tmp_path / "f"
    folder.mkdir()
    shutil.copy(samples[2], folder / "a.mp4")
    result = appmod.app.test_cli_runner().invoke(
        args=["import-folder", str(folder), "--user", "coach"])
    assert result.exit_code == 1
    assert "license is required" in result.output
    assert query(appmod, "SELECT COUNT(*) FROM videos")[0][0] == 0


def test_folder_import_unknown_user(appmod, tmp_path):
    result = appmod.app.test_cli_runner().invoke(
        args=["import-folder", str(tmp_path), "--user", "ghost", "--license", "x"])
    assert result.exit_code != 0 and "unknown user" in result.output


def test_storage_prefix_import_adopts_objects_in_place(appmod, samples):
    make_user(appmod, "coach")
    for i, name in enumerate(("inbox/a.mp4", "inbox/sub/b.mp4")):
        with open(samples[i + 3], "rb") as f:
            appmod.storage.save(f, name)
    appmod.storage.save(io.BytesIO(b"x"), "inbox/readme.txt")
    runner = appmod.app.test_cli_runner()
    result = runner.invoke(args=["import-storage", "inbox/", "--user", "coach",
                                 "--license", "own-footage", "--process"])
    assert result.exit_code == 0, result.output
    assert "2 queued" in result.output
    originals = sorted(r[0] for r in query(appmod, "SELECT uri FROM assets WHERE kind='original'"))
    assert originals == ["inbox/a.mp4", "inbox/sub/b.mp4"]
    assert query(appmod, "SELECT COUNT(*) FROM assets WHERE kind='mezzanine'")[0][0] == 2
    # Re-running skips them; the empty prefix (whole store) is refused.
    assert "0 queued, 2 skipped" in runner.invoke(args=[
        "import-storage", "inbox/", "--user", "coach", "--license", "x"]).output
    assert runner.invoke(args=["import-storage", "", "--user", "coach",
                               "--license", "x"]).exit_code != 0


def test_run_jobs_command(appmod, tmp_path, samples):
    make_user(appmod, "coach")
    folder = tmp_path / "f"
    folder.mkdir()
    shutil.copy(samples[4], folder / "a.mp4")
    runner = appmod.app.test_cli_runner()
    runner.invoke(args=["import-folder", str(folder), "--user", "coach", "--license", "x"])
    assert "ran 4 processing jobs" in runner.invoke(args=["run-jobs"]).output


def api_import(client, **body):
    return client.post("/api/import", json=body,
                       headers={"Authorization": "Bearer test-token"})


def test_api_import_runs_pipeline_and_requires_license(appmod, client, samples):
    make_user(appmod, "phone")
    with open(samples[5], "rb") as f:
        appmod.storage.save(f, "abc123.mp4")
    appmod.storage.save(io.BytesIO(b"jpeg"), "abc123.jpg")
    base = dict(username="phone", stored_name="abc123.mp4", source_id="icloud:1",
                thumbnail_name="abc123.jpg")
    assert api_import(client, **base).status_code == 400  # no license
    resp = api_import(client, license="own-footage", source="iCloud", **base)
    assert resp.status_code == 201
    vid = resp.get_json()["id"]
    assert api_import(client, license="own-footage", **base).get_json() == {
        "id": vid, "status": "exists"}
    jobs.run_pending(appmod.pipeline)
    video = query(appmod, "SELECT * FROM videos WHERE id=?", (vid,))[0]
    assert video["status"] == "ready" and video["source"] == "iCloud"
    assert video["thumbnail_name"] == f"videos/{vid}/thumbnail-v1.jpg"
    kinds = {r[0] for r in query(appmod, "SELECT kind FROM assets WHERE video_id=?", (vid,))}
    assert {"original", "mezzanine", "proxy", "keyframe", "thumbnail"} <= kinds
    # Deleting the video removes the adopted blob and the pipeline's poster.
    import db
    conn = db.connect(appmod.app.config["DATABASE"])
    user_id = conn.execute("SELECT id FROM users WHERE username='phone'").fetchone()[0]
    conn.close()
    from conftest import login
    csrf = login(client, user_id)
    client.post(f"/delete/{vid}", data={"csrf_token": csrf})
    assert not appmod.storage.exists("abc123.mp4") and not appmod.storage.exists("abc123.jpg")
    assert appmod.storage.list(f"videos/{vid}/") == []


def test_api_import_auth_and_validation(appmod, client):
    assert client.post("/api/import", json={}).status_code == 401
    make_user(appmod, "phone")
    bad = api_import(client, username="phone", stored_name="../x.mp4",
                     source_id="s", license="l")
    assert bad.status_code == 400
    missing = api_import(client, username="phone", stored_name="nope.mp4",
                         source_id="s", license="l")
    assert missing.status_code == 409


def test_icloud_script_sends_license(tmp_path, samples):
    """The bundled sync script registers videos with a license."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_icloud", os.path.join(os.path.dirname(__file__), "..", "pipeline", "sync_icloud.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    folder = tmp_path / "icloud"
    folder.mkdir()
    shutil.copy(samples[6], folder / "IMG_0001.MOV")
    payloads = []
    summary = mod.sync(str(folder), lambda *a: 10,
                       lambda p: payloads.append(p) or (201, {"id": "v", "status": "created"}),
                       str(tmp_path / "manifest.json"), "phone", license="competitor-consent")
    assert summary["new"] == 1
    assert payloads[0]["license"] == "competitor-consent" and payloads[0]["source"] == "iCloud"
