"""Video file storage backends.

LocalStorage keeps files on disk (good for development and single-instance
deployments). AzureBlobStorage stores files in an Azure Blob container and
serves them to players via short-lived SAS URLs, which lets the browser
stream directly from Azure with full range-request support.

The backend is chosen at startup: if AZURE_STORAGE_CONNECTION_STRING is set,
Azure Blob Storage is used; otherwise files go to the local uploads folder.

Keys may contain "/" (e.g. ``videos/<id>/original.mp4``). Originals and
derivatives written by the processing pipeline use ``overwrite=False`` so an
existing object is never replaced (STO-1); a write-once save raises
FileExistsError instead. Writes are atomic: a key either holds the complete
object or doesn't exist.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone


def _check_key(stored_name):
    """Reject keys that could escape the storage root."""
    if (
        not stored_name
        or stored_name.startswith("/")
        or "\\" in stored_name
        or any(part in ("", ".", "..") for part in stored_name.split("/"))
    ):
        raise ValueError(f"invalid storage key: {stored_name!r}")
    return stored_name


class LocalStorage:
    """Stores videos on the local filesystem."""

    is_remote = False

    def __init__(self, upload_dir):
        self.upload_dir = upload_dir
        os.makedirs(upload_dir, exist_ok=True)

    def save(self, stream, stored_name, content_type=None, overwrite=True):
        """Write stream to stored_name; returns the size in bytes.

        content_type is accepted for interface parity with Azure and ignored.
        With overwrite=False an existing key raises FileExistsError.
        """
        path = self.path(stored_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not overwrite and os.path.exists(path):
            raise FileExistsError(stored_name)
        # Write to a temp file in the same directory, then move it into place
        # so readers never see a partial object.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            if overwrite:
                os.replace(tmp, path)
            else:
                # link() fails if the target exists, closing the race between
                # the check above and the write.
                os.link(tmp, path)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
        return os.path.getsize(path)

    def path(self, stored_name):
        return os.path.join(self.upload_dir, *_check_key(stored_name).split("/"))

    def exists(self, stored_name):
        return os.path.exists(self.path(stored_name))

    def size(self, stored_name):
        return os.path.getsize(self.path(stored_name))

    def list(self, prefix=""):
        """Return all keys starting with prefix, sorted."""
        keys = []
        for root, _dirs, files in os.walk(self.upload_dir):
            for name in files:
                if name.startswith(".tmp-"):
                    continue
                rel = os.path.relpath(os.path.join(root, name), self.upload_dir)
                key = rel.replace(os.sep, "/")
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def download_to(self, stored_name, local_path):
        """Copy an object to a local file path."""
        with open(self.path(stored_name), "rb") as src, open(local_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

    def delete(self, stored_name):
        try:
            os.remove(self.path(stored_name))
        except FileNotFoundError:
            pass


class AzureBlobStorage:
    """Stores videos in an Azure Blob Storage container."""

    is_remote = True

    def __init__(self, connection_string, container_name="videos"):
        from azure.storage.blob import BlobServiceClient

        self.service = BlobServiceClient.from_connection_string(connection_string)
        self.container_name = container_name
        self.container = self.service.get_container_client(container_name)
        if not self.container.exists():
            self.container.create_container()

    def save(self, stream, stored_name, content_type="application/octet-stream",
             overwrite=True):
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        blob = self.container.get_blob_client(_check_key(stored_name))
        try:
            # overwrite=False makes the service refuse an existing blob
            # (If-None-Match: *), which is atomic across writers.
            blob.upload_blob(
                stream,
                overwrite=overwrite,
                content_settings=ContentSettings(
                    content_type=content_type or "application/octet-stream"
                ),
            )
        except ResourceExistsError:
            raise FileExistsError(stored_name) from None
        return blob.get_blob_properties().size

    def exists(self, stored_name):
        return self.container.get_blob_client(stored_name).exists()

    def size(self, stored_name):
        return self.container.get_blob_client(stored_name).get_blob_properties().size

    def list(self, prefix=""):
        return sorted(b.name for b in self.container.list_blobs(name_starts_with=prefix))

    def download_to(self, stored_name, local_path):
        with open(local_path, "wb") as f:
            self.container.get_blob_client(stored_name).download_blob().readinto(f)

    def playback_url(self, stored_name, expires_in_hours=2):
        """Return a short-lived read-only SAS URL for direct streaming."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        sas = generate_blob_sas(
            account_name=self.service.account_name,
            container_name=self.container_name,
            blob_name=stored_name,
            account_key=self.service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
        )
        return f"{self.container.url}/{stored_name}?{sas}"

    def delete(self, stored_name):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.container.delete_blob(stored_name)
        except ResourceNotFoundError:
            pass


def create_storage(config):
    conn_str = config.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str:
        return AzureBlobStorage(conn_str, config.get("AZURE_CONTAINER_NAME", "videos"))
    return LocalStorage(config["UPLOAD_DIR"])
