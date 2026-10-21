"""The pipeline against a remote (Azure-like) backend: no local paths, so
stages must download originals and cache their outputs in the work dir."""
import io
import os

import pytest

import jobs
from conftest import FFMPEG, query
from test_pipeline import upload

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


class MemoryRemoteStorage:
    """Stands in for AzureBlobStorage: same interface, objects in memory."""

    is_remote = True

    def __init__(self):
        self.objects = {}
        self.downloads = []

    def save(self, stream, name, content_type="application/octet-stream", overwrite=True):
        if not overwrite and name in self.objects:
            raise FileExistsError(name)
        self.objects[name] = stream.read()
        return len(self.objects[name])

    def exists(self, name):
        return name in self.objects

    def size(self, name):
        return len(self.objects[name])

    def list(self, prefix=""):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def download_to(self, name, local_path):
        self.downloads.append(name)
        with open(local_path, "wb") as f:
            f.write(self.objects[name])

    def delete(self, name):
        self.objects.pop(name, None)

    def playback_url(self, name, expires_in_hours=2):
        return f"https://blob.example/{name}?sas=1"


def test_pipeline_on_remote_storage(appmod, logged_in, samples, monkeypatch):
    remote = MemoryRemoteStorage()
    monkeypatch.setattr(appmod, "storage", remote)
    monkeypatch.setattr(appmod.pipeline, "storage", remote)
    client, _, csrf = logged_in

    vid = upload(client, csrf, samples[0])
    jobs.run_pending(appmod.pipeline)
    assert set(dict(query(appmod, "SELECT stage, status FROM processing_jobs")).values()) == {"succeeded"}
    keys = remote.list(f"videos/{vid}/")
    assert f"videos/{vid}/original.mp4" in keys
    assert f"videos/{vid}/mezzanine-v1.mp4" in keys and f"videos/{vid}/proxy480-v1.mp4" in keys
    assert any("/keyframes-v1/" in k for k in keys) and f"videos/{vid}/thumbnail-v1.jpg" in keys
    # The staged upload was cached locally, so nothing had to be downloaded.
    assert remote.downloads == []
    assert os.listdir(appmod.pipeline.work_root) == []

    page = client.get(f"/watch/{vid}").data.decode()
    assert f"https://blob.example/videos/{vid}/mezzanine-v1.mp4" in page
    assert client.get(f"/thumb/{vid}").status_code == 302


def test_storage_import_on_remote_downloads_once(appmod, samples, monkeypatch):
    from conftest import make_user
    remote = MemoryRemoteStorage()
    monkeypatch.setattr(appmod, "storage", remote)
    monkeypatch.setattr(appmod.pipeline, "storage", remote)
    make_user(appmod, "coach")
    with open(samples[1], "rb") as f:
        remote.save(io.BytesIO(f.read()), "inbox/match.mp4")
    result = appmod.app.test_cli_runner().invoke(args=[
        "import-storage", "inbox/", "--user", "coach", "--license", "x", "--process"])
    assert "1 queued" in result.output, result.output
    assert set(dict(query(appmod, "SELECT stage, status FROM processing_jobs")).values()) == {"succeeded"}
    # The adopted original is downloaded once and reused by later stages.
    assert remote.downloads == ["inbox/match.mp4"]
