# BJJ Video Hosting

A simple Brazilian Jiu-Jitsu video hosting website built with Python (Flask),
designed to deploy to Azure App Service with videos stored in Azure Blob
Storage.

## Features

- **Accounts** — sign up, log in, and log out (session-based auth with
  hashed passwords and CSRF-protected forms); each profile has a **BJJ belt**
  (white → blue → purple → brown → black) shown as a small icon next to the
  user's name on their profile, comments, and video credits
- Upload videos (MP4, WebM, Ogg, MOV, M4V) with a title, description and
  **match details** — competition, date, competitors ("Name (Team)"),
  source and a **required license** — requires an account; each video is
  owned by its uploader
- **Resumable uploads up to 20 GB** — the upload page sends files in
  chunks, retries dropped connections and resumes where it left off (even
  after closing the page: pick the same file again)
- **Bulk import** from a server folder or a storage prefix (e.g. a blob
  container `inbox/`), with an optional CSV manifest of per-file metadata
- **Processing pipeline** — every video is hashed (exact duplicates are
  flagged, not reprocessed), its original stored write-once, and converted
  to a standard H.264 copy (≤1080p, used for playback) plus a 480p analysis
  proxy; keyframes are extracted at every shot change and at least every
  10 s. Jobs run in the background, retry automatically and survive
  restarts
- **Job dashboard** (`/jobs`) — per-video stage status, attempts, errors
  and a Retry button
- Browse all videos in a grid, with search; anyone can watch
- **Thumbnails** — a poster frame is extracted from each video with ffmpeg
  on upload (and by the import pipeline) and shown on the grid and as the
  player's poster
- Watch pages with an HTML5 player, view counts, uploader credit, and a
  delete button shown only to the owner
- **Comments** — logged-in users can comment on any video under their
  username; you can delete your own comments
- **Large uploads** — anything over 500 MB (or not browser-playable) shows
  as "Processing…" until its H.264 copy is ready; smaller ones play from the
  original within seconds. ffmpeg ships with the app via `imageio-ffmpeg`,
  so this works on Azure App Service with no extra setup.
- **Bulk import from iCloud** — a local pipeline
  ([`pipeline/`](pipeline/README.md)) uploads videos straight from your
  iCloud library to blob storage and registers them via a token-auth
  `POST /api/import` endpoint, after which they're processed like any
  upload. Idempotent, so re-running only picks up new videos.
- An account page listing your own uploads
- Two storage backends, selected automatically:
  - **Local disk** (default) — with HTTP Range support so seeking works
  - **Azure Blob Storage** — used when `AZURE_STORAGE_CONNECTION_STRING` is
    set; players stream directly from Azure via short-lived SAS URLs
- Users and video metadata in SQLite (kept on the App Service persistent
  `/home` share)

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Open http://127.0.0.1:5000. Uploads go to `instance/uploads/` and metadata to
`instance/videos.db`. Processing jobs run on a background thread started by
the first request; watch them at http://127.0.0.1:5000/jobs.

### Bulk import

```bash
# Every video under a folder (recursively), owned by an existing user.
flask --app app import-folder ~/footage/worlds-2026 --user coach \
    --license own-footage --competition "IBJJF Worlds 2026" --event-date 2026-06-01

# Per-file metadata from a CSV with a `file` column (path relative to the
# folder, or file name) and any of: title, description, competition,
# event_date, participants, source, license.
flask --app app import-folder ~/footage --user coach --manifest matches.csv

# Everything under a prefix in the app's storage — on Azure, blobs you
# dropped into the container with azcopy/Storage Explorer.
flask --app app import-storage inbox/ --user coach --license event-licensed
```

Add `--process` to run the processing jobs right away in the command
(otherwise the web app's worker picks them up), or run them any time with
`flask --app app run-jobs`. Re-running an import skips files already
imported. Folder imports read files in place, so leave them until their
ingest job has finished.

### Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

The suite is offline and uses ffmpeg to generate small sample clips; it
includes the Phase 0 "Gate 1" check (`tests/test_gate1.py`: ten videos
ingested end to end through every ingestion path).

## Deploy to Azure (one script, push-to-deploy)

Open [Azure Cloud Shell](https://shell.azure.com) (Bash) and run:

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
./deploy/azure-setup.sh <app-name> <owner>/<repo>
```

For example:

```bash
./deploy/azure-setup.sh myvideohost dgc20/VideoHostingSite
```

The script does everything in one run:

1. Creates a resource group, a storage account with a `videos` blob
   container, a Linux App Service plan (**F1 Free tier** — $0/month), and a
   Python 3.12 web app, with the connection string and gunicorn startup
   command wired in.
2. Configures GitHub Actions push-to-deploy (the same mechanism as the Azure
   Portal's Deployment Center): it sets up OIDC and commits a single
   workflow, `main_<app-name>.yml`, that deploys on every push to `main`.
   It prompts once to authorize GitHub.

After it finishes, **every push to `main` deploys automatically** (watch the
repo's Actions tab), and the first deploy has already been kicked off. Your
site goes live at `https://<app-name>.azurewebsites.net`.

> If you set the app up through the Azure Portal instead, its Deployment
> Center produces the same `main_<app-name>.yml` workflow — so don't also
> commit a second deploy workflow, or every push will deploy twice.

To use a paid tier instead of Free:

```bash
./deploy/azure-setup.sh <app-name> <owner>/<repo> eastus B1   # ~$13/month
```
## Configuration

All optional, via environment variables (App Settings on Azure):

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | unset | When set, videos are stored in Azure Blob Storage |
| `AZURE_CONTAINER_NAME` | `videos` | Blob container name |
| `SECRET_KEY` | dev value | Signs session cookies — **set a strong random value in production** (the setup script does this) |
| `SESSION_COOKIE_SECURE` | `0` | Set to `1` to send the session cookie only over HTTPS (the setup script sets this on Azure) |
| `MAX_UPLOAD_MB` | `2048` | Maximum size of a single request (the non-JavaScript upload form); the resumable uploader uses `MAX_RESUMABLE_UPLOAD_GB` |
| `COMPRESS_THRESHOLD_MB` | `500` | Originals larger than this aren't played directly; they show "Processing…" until the H.264 copy is ready |
| `IMPORT_API_TOKEN` | unset | Bearer token enabling `POST /api/import` for the iCloud pipeline; disabled when unset |
| `DATA_DIR` | `/home/data` on Azure, `instance/` locally | Where SQLite, local uploads, staged uploads and processing work files live |
| `MAX_RESUMABLE_UPLOAD_GB` | `20` | Largest file accepted by the resumable uploader (needs that much free disk in `DATA_DIR`) |
| `UPLOAD_CHUNK_MB` | `8` | Largest chunk per resumable-upload request |
| `UPLOAD_SESSION_TTL_HOURS` | `48` | Unfinished resumable uploads idle this long are discarded |
| `JOB_WORKER` | `1` | Run the background processing worker in the web app; `0` to run jobs only via `flask run-jobs` |
| `JOB_MAX_ATTEMPTS` | `3` | Automatic attempts per processing stage before it's marked failed |
| `JOB_LEASE_SECONDS` | `120` | How long a stage may go without a heartbeat before another worker takes it over (crash recovery) |
| `KEYFRAME_INTERVAL_S` | `10` | Keyframe at least this often (in addition to one per shot change) |
| `KEYFRAME_SCENE_THRESHOLD` | `0.4` | ffmpeg scene-change score (0–1) that counts as a new shot |
| `ADMIN_USERNAMES` | unset | Comma-separated usernames who see and can retry every video's jobs on `/jobs` |

## Cost

The setup is tuned for minimal spend:

| Resource | Tier | Cost |
|---|---|---|
| App Service plan | F1 Free (default) | **$0/month** |
| Blob Storage | Standard LRS, Hot tier | ~$0.02/GB/month stored |
| Bandwidth (egress) | — | First 100 GB/month free, then ~$0.087/GB |

- **The web app costs nothing.** Because players stream video straight from
  Blob Storage via SAS URLs, the Flask app only serves small HTML pages —
  the Free tier's 60 CPU-minutes/day quota is plenty. The trade-off is no
  Always On: after ~20 idle minutes the app sleeps and the next visit takes
  a few seconds to wake it. If that bothers you, `B1` (~$13/month) removes
  the quotas.
- **Storage is pennies.** 100 GB of videos is about $2/month. LRS is the
  cheapest redundancy, and Hot is deliberately chosen over Cool/Cold: the
  colder tiers charge per-GB retrieval fees, so for videos that actually get
  watched they cost *more*, not less.
- **Bandwidth is the only cost that scales with popularity.** Every video
  view is egress from Blob Storage. 100 GB/month free covers roughly
  1,000 views of a 100 MB video; beyond that it's ~$8.70 per additional
  100 GB. If the site becomes high-traffic, put Azure CDN or Front Door in
  front of the blob container rather than scaling the web app.
- SQLite instead of a managed database saves the ~$13+/month a database
  service would add — there is nothing else billable in this setup.

## Notes

- Accounts are self-service: anyone can sign up, then upload videos and
  delete their own. There's no email verification or admin role — add those
  if you need them. Watching is open to everyone (no login required).
- SQLite is fine for a single App Service instance. If you scale out to
  multiple instances, move users and metadata to Azure Database for
  PostgreSQL, and switch sessions to a shared store.
- Every upload and import needs a **license** (e.g. `own-footage`,
  `event-licensed`); footage without one is refused. `/api/import` callers
  must send `license` — the bundled iCloud script does (`--license`).
- Storage layout: each video's files live under `videos/<id>/`
  (`original.<ext>`, `mezzanine-v1.mp4`, `proxy480-v1.mp4`,
  `keyframes-v1/<ms>.jpg`, `thumbnail-v1.jpg`) and are never overwritten;
  the `-v1` suffix lets a future pipeline version write alongside them.
  Deleting a video removes all of them.
- Processing happens on the web app's own CPU. A 500 MB+ transcode takes
  a while on the Free tier and eats into its 60 CPU-minutes/day quota — if
  people upload large videos regularly, use `B1` or higher. Large uploads
  also need temp disk space in `DATA_DIR` (Free tier has 1 GB; `B1` has
  10 GB). Interrupted compressions resume automatically after a restart.
