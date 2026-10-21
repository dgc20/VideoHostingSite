"""STO-1: originals and derivatives in object storage, immutable."""
import io
from types import SimpleNamespace

import pytest

import storage


@pytest.fixture
def local(tmp_path):
    return storage.LocalStorage(str(tmp_path / "store"))


def test_nested_keys_round_trip(local, tmp_path):
    size = local.save(io.BytesIO(b"abc"), "videos/v1/original.mp4")
    assert size == 3
    assert local.exists("videos/v1/original.mp4")
    assert local.size("videos/v1/original.mp4") == 3
    out = tmp_path / "copy.bin"
    local.download_to("videos/v1/original.mp4", str(out))
    assert out.read_bytes() == b"abc"


def test_write_once_refuses_overwrite(local):
    local.save(io.BytesIO(b"first"), "videos/v1/original.mp4", overwrite=False)
    with pytest.raises(FileExistsError):
        local.save(io.BytesIO(b"second"), "videos/v1/original.mp4", overwrite=False)
    with open(local.path("videos/v1/original.mp4"), "rb") as f:
        assert f.read() == b"first"


def test_default_save_still_overwrites_for_legacy_callers(local):
    local.save(io.BytesIO(b"one"), "abc.mp4")
    local.save(io.BytesIO(b"two"), "abc.mp4")
    assert local.size("abc.mp4") == 3


def test_list_by_prefix_skips_temp_files(local):
    for key in ("inbox/a.mp4", "inbox/sub/b.mp4", "videos/x/original.mp4"):
        local.save(io.BytesIO(b"x"), key)
    assert local.list("inbox/") == ["inbox/a.mp4", "inbox/sub/b.mp4"]
    assert len(local.list()) == 3


@pytest.mark.parametrize("key", ["../evil", "/abs", "a//b", "a/../b", "a\\b", ""])
def test_rejects_unsafe_keys(local, key):
    with pytest.raises(ValueError):
        local.path(key)


class _FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, stream, overwrite, content_settings):
        from azure.core.exceptions import ResourceExistsError

        if not overwrite and self.name in self.store:
            raise ResourceExistsError("exists")
        self.store[self.name] = stream.read()

    def get_blob_properties(self):
        return SimpleNamespace(size=len(self.store[self.name]))

    def exists(self):
        return self.name in self.store

    def download_blob(self):
        data = self.store[self.name]
        return SimpleNamespace(readinto=lambda f: f.write(data))


class _FakeContainer:
    def __init__(self):
        self.store = {}

    def get_blob_client(self, name):
        return _FakeBlob(self.store, name)

    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=n) for n in self.store if n.startswith(name_starts_with)]


def test_azure_backend_write_once_and_listing(tmp_path):
    """The Azure backend is exercised against an in-memory fake container."""
    az = storage.AzureBlobStorage.__new__(storage.AzureBlobStorage)
    az.container = _FakeContainer()
    assert az.save(io.BytesIO(b"abcd"), "videos/v/original.mp4", "video/mp4", overwrite=False) == 4
    with pytest.raises(FileExistsError):
        az.save(io.BytesIO(b"zz"), "videos/v/original.mp4", overwrite=False)
    assert az.exists("videos/v/original.mp4")
    assert az.size("videos/v/original.mp4") == 4
    assert az.list("videos/") == ["videos/v/original.mp4"]
    out = tmp_path / "dl"
    az.download_to("videos/v/original.mp4", str(out))
    assert out.read_bytes() == b"abcd"
