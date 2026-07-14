# VideoHost

A simple video hosting website built with Python (Flask), designed to deploy
to Azure App Service with videos stored in Azure Blob Storage.

## Features

- Upload videos (MP4, WebM, Ogg, MOV, M4V) with a title and description
- Browse all videos in a grid, with search
- Watch pages with an HTML5 player, view counts, and delete
- Two storage backends, selected automatically:
  - **Local disk** (default) — with HTTP Range support so seeking works
  - **Azure Blob Storage** — used when `AZURE_STORAGE_CONNECTION_STRING` is
    set; players stream directly from Azure via short-lived SAS URLs
- Video metadata in SQLite (kept on the App Service persistent `/home` share)

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Open http://127.0.0.1:5000. Uploads go to `instance/uploads/` and metadata to
`instance/videos.db`.

## Deploy to Azure

Prerequisite: [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
logged in with `az login`.

### 1. Provision the infrastructure

```bash
./deploy/azure-setup.sh <app-name>        # e.g. ./deploy/azure-setup.sh myvideohost
```

This creates a resource group, a storage account with a `videos` blob
container, a Linux App Service plan (B1), and a Python 3.12 web app — and
wires the connection string and gunicorn startup command into the app.

### 2. Deploy the code

Either push directly from your machine:

```bash
az webapp up --name <app-name> --resource-group <app-name>-rg
```

Or use the included GitHub Actions workflow
(`.github/workflows/azure-deploy.yml`), which deploys on every push to
`main`:

1. Set the repository **variable** `AZURE_WEBAPP_NAME` to your app name.
2. Save the publish profile as the repository **secret**
   `AZURE_WEBAPP_PUBLISH_PROFILE`:
   ```bash
   az webapp deployment list-publishing-profiles \
     --name <app-name> --resource-group <app-name>-rg --xml
   ```

Your site is then live at `https://<app-name>.azurewebsites.net`.

## Configuration

All optional, via environment variables (App Settings on Azure):

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | unset | When set, videos are stored in Azure Blob Storage |
| `AZURE_CONTAINER_NAME` | `videos` | Blob container name |
| `SECRET_KEY` | dev value | Flask session/flash signing key — set a real one in production |
| `MAX_UPLOAD_MB` | `512` | Maximum upload size |
| `DATA_DIR` | `/home/data` on Azure, `instance/` locally | Where SQLite (and local uploads) live |

## Notes

- The site is intentionally auth-free; anyone with the URL can upload and
  delete. Add authentication before using it beyond personal/demo purposes.
- SQLite is fine for a single App Service instance. If you scale out to
  multiple instances, move metadata to Azure Database for PostgreSQL.
