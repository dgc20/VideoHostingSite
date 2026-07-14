"""Video file storage backends.

LocalStorage keeps files on disk (good for development and single-instance
deployments). AzureBlobStorage stores files in an Azure Blob container and
serves them to players via short-lived SAS URLs, which lets the browser
stream directly from Azure with full range-request support.

The backend is chosen at startup: if AZURE_STORAGE_CONNECTION_STRING is set,
Azure Blob Storage is used; otherwise files go to the local uploads folder.
"""
import os
from datetime import datetime, timedelta, timezone


class LocalStorage:
    """Stores videos on the local filesystem."""

    is_remote = False

    def __init__(self, upload_dir):
        self.upload_dir = upload_dir
        os.makedirs(upload_dir, exist_ok=True)

    def save(self, stream, stored_name):
        path = self.path(stored_name)
        with open(path, "wb") as f:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return os.path.getsize(path)

    def path(self, stored_name):
        return os.path.join(self.upload_dir, stored_name)

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

    def save(self, stream, stored_name, content_type="application/octet-stream"):
        from azure.storage.blob import ContentSettings

        blob = self.container.get_blob_client(stored_name)
        blob.upload_blob(
            stream,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        return blob.get_blob_properties().size

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
