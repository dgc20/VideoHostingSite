# iCloud → Blob Storage sync pipeline

Bulk-import videos from your iCloud library into BJJ Video Hosting, and
re-run it any time to pick up only the new ones.

## Why it works this way

iCloud has **no server-side API** — Apple doesn't let a hosted service log in
and pull your library. So this pipeline runs **on your own machine** (where
you're signed into iCloud) and does three things per video:

1. Gets the file onto local disk (from a synced folder, or via `icloudpd`).
2. Uploads it **directly to your Azure Blob container** — this skips the web
   app entirely, so there's no request-size limit or 230-second timeout.
3. Calls the site's `POST /api/import` endpoint to register the video so it
   shows up on the site (owned by the account you choose).

A local **manifest** (`bjjvid_sync_manifest.json`) records what's already been
synced, keyed by filename + size, so re-running only uploads new videos. The
server also de-duplicates by the same id, so nothing is imported twice even
if you lose the manifest.

## One-time server setup

The `/api/import` endpoint is disabled until you set a token on the web app.
Pick a strong random value and set it as an App Setting:

```bash
az webapp config appsettings set --name <app-name> --resource-group <app-name>-rg \
  --settings IMPORT_API_TOKEN="$(openssl rand -hex 32)"
```

(`deploy/azure-setup.sh` sets one automatically for new deployments and prints
it.) You'll use the same value as `IMPORT_API_TOKEN` below.

## Install

On the machine you'll run it from:

```bash
pip install -r pipeline/requirements.txt
```

## Configure

```bash
export SITE_URL="https://<app-name>.azurewebsites.net"
export IMPORT_API_TOKEN="<the token you set on the server>"
export IMPORT_USERNAME="<your account username on the site>"
export AZURE_STORAGE_CONNECTION_STRING="<same connection string the app uses>"
# export AZURE_CONTAINER_NAME="videos"   # only if you changed it
```

Get the storage connection string with:

```bash
az storage account show-connection-string \
  --name <app-name>store --resource-group <app-name>-rg --query connectionString -o tsv
```

## Run

**From a folder you already have** (Mac Photos export, iCloud for Windows
downloads, etc.):

```bash
python pipeline/sync_icloud.py --source ~/Pictures/iCloudVideos
```

**Straight from iCloud** (drives `icloudpd`, prompts for your Apple ID
password + 2FA the first time):

```bash
python pipeline/sync_icloud.py --icloud --icloud-user you@icloud.com
```

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | List what would be synced without uploading |
| `--compress` | Transcode each video (H.264/AAC, ≤1080p) before upload |
| `--manifest PATH` | Use a specific manifest file |
| `--icloudpd-arg ...` | Pass an extra flag through to `icloudpd` (repeatable) |
| `-v` | Verbose logging |

Non-web formats (e.g. `.avi`, `.mkv`) are transcoded to MP4 automatically so
browsers can play them; `--compress` transcodes everything.

## Getting iCloud videos onto disk

- **macOS** — In Photos, select your videos → File → Export → *Export
  Unmodified Original*, into a folder, then `--source` that folder. Or use
  [`osxphotos`](https://github.com/RhetTbull/osxphotos):
  `osxphotos export ~/Pictures/iCloudVideos --only-movies`.
- **Windows** — Install *iCloud for Windows*; it downloads originals to
  `…\Pictures\iCloud Photos\Downloads`. Point `--source` there.
- **Any OS** — Use `--icloud` (this script calls `icloudpd`). Note it's an
  unofficial tool that logs in with your Apple ID; treat it accordingly.

## Refreshing on a schedule

Because it's idempotent, just re-run it. To automate, add a cron job (macOS/
Linux) or Task Scheduler entry (Windows) that runs the same command — e.g.
nightly:

```cron
0 2 * * *  cd /path/to/repo && /usr/bin/python3 pipeline/sync_icloud.py --source ~/Pictures/iCloudVideos >> ~/bjjvid-sync.log 2>&1
```
