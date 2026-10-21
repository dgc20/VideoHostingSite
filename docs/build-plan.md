# Build plan: Phase 0 (Foundations, weeks 1–4)

Source: *Competitive Footage Analysis Pipeline — Spec Sheet*
(claude.ai doc `f2489904-915e-46b2-b0b5-801d2ae9ee01`), including the
architecture diagram (widget `a6633051-6c95`) and the roadmap (widget
`a0f1a3df-f3e7`).

## 1. Spec inventory

**Architecture (diagram).** Footage sources (uploads, buckets, URLs) feed
four stages: **1 Ingest** (upload, bulk import, metadata + license, dedupe by
hash, queue processing job) → **2 Process** (transcode + proxy, shot
boundaries, keyframes, audio extraction) → **3 Enrich** (STT, OCR, detection,
captions, events, embeddings) → **4 Serve** (REST + MCP, search, timelines,
frames/clips, annotations). Stages 1–3 are async jobs; stage 4 is always-on.
Stages 2 and 3 read/write **shared storage** (object store for video and
frames; relational DB + vectors for metadata, events, embeddings). Agents
only talk to stage 4.

**Roadmap.** Phase 0 · Foundations (weeks 1–4): *Storage + Postgres; Upload,
bulk import; Transcode, keyframes; Job dashboard*. **Gate 1: 10 videos
ingested end to end.**

**Requirements in scope for Phase 0**

| ID | Requirement | Priority |
|---|---|---|
| ING-1 | Direct upload, resumable, up to 20 GB per file | P0 |
| ING-2 | Bulk import from a bucket or folder | P0 |
| ING-4 | Metadata on upload: competition, date, teams/players, source, license | P0 |
| ING-5 | Dedupe by content hash; flag near-duplicates by perceptual hash | P1 (optional) |
| PRE-1 | Transcode to a mezzanine (H.264, 1080p max) plus a 480p proxy | P0 |
| PRE-3 | Keyframes: 1 per shot plus 1 every N seconds | P0 |
| STO-1 | Originals and derivatives in object storage, immutable | P0 |
| STO-2 | Metadata, segments and events in a relational DB | P0 |
| OPS-1 | Job dashboard: per-video stage status, retries, failures | P0 |

Related spec rules used here: *"Each video records its source and license;
ingestion refuses files without one"* (Legal); *"Every derived record stores
the model and pipeline version"* (NFR Reproducibility); data-model entities
**Video**, **Asset**, **ProcessingJob** (Segment/Event/etc. are later phases).

**Out of scope (later phases):** ING-3 (URL import), PRE-2 (persisted shot
segments via PySceneDetect), PRE-4 (audio), all ENR-*, STO-3, AGT-*, OPS-2,
OPS-3, the REST/MCP agent API, per-agent auth, audit log.

## 2. Existing codebase (survey)

Flask monolith (`app.py`, all routes), SQLite through `db.py` (schema string
plus additive `_migrate_*`), `storage.py` with `LocalStorage` and
`AzureBlobStorage`, ffmpeg via `imageio-ffmpeg` in `processing.py` (daemon
thread compression + `resume_pending`), session auth + CSRF in `auth.py`, a
token-auth `POST /api/import` used by the local iCloud script in `pipeline/`.
Deployed as one gunicorn worker with 8 threads on Azure App Service. No tests
or lint config existed.

## 3. Spec → code mapping

| Spec component | Where it lives | Notes |
|---|---|---|
| Object storage (STO-1) | `storage.py` (extended) | Keys `videos/<id>/…`; write-once `save(..., overwrite=False)`; `exists/size/list/download_to` on both backends |
| Relational DB (STO-2) | `db.py` (extended) | New columns on `videos`; new tables `video_participants`, `assets`, `processing_jobs`, `upload_sessions` |
| Ingest: metadata + license (ING-4) | `ingest.py` | One validator used by every entry point |
| Ingest: resumable upload (ING-1) | `ingest.py` + routes in `app.py` + `static/upload.js` | Session auth + CSRF header; chunks staged on local disk |
| Ingest: bulk import (ING-2) | `ingest.py` + Flask CLI commands in `app.py` | Folder (+ optional CSV manifest) or storage prefix (local dir or Azure container) |
| Ingest: dedupe (ING-5, exact only) | `stages.py` ingest stage | SHA-256 content hash |
| Job queue / worker | `jobs.py` | SQLite-backed queue, leases, retries, one daemon thread (like `processing.py`) |
| Process: transcode + proxy (PRE-1) | `processing.py` (ffmpeg primitives) + `stages.py` | One ffmpeg pass, two outputs |
| Process: keyframes (PRE-3) | `processing.py` + `stages.py` | One ffmpeg pass: scene changes + at-least-every-N-seconds |
| Job dashboard (OPS-1) | `/jobs` routes in `app.py`, `templates/jobs.html` | Per-video stage chips, attempts, errors, retry |

**Stack gaps (spec vs. repo), resolved toward the repo as instructed:**
S3/GCS → existing Local/Azure Blob backends; Postgres → SQLite with a
Postgres-friendly schema; Temporal/SQS → DB-backed queue + daemon thread;
FastAPI → existing Flask app; PySceneDetect → ffmpeg `scene` score for the
"1 per shot" keyframes (persisted shot segments stay in PRE-2).

## 4. Schema changes and migrations

All additive, applied by `db._migrate` on startup (existing databases keep
working). New tables use app-generated TEXT ids and app-written UTC
timestamps (no SQLite-only defaults), TEXT JSON columns (→ `JSONB`), and
declared `REFERENCES`, so a later Postgres move is a type-for-type port.

- `videos` + `competition`, `event_date` (ISO date), `source`, `license`,
  `duration_ms`, `fps`, `width`, `height`, `content_hash`, `duplicate_of`.
  Status values: `processing`, `ready`, `failed`, `duplicate`.
- `video_participants(video_id, position, name, team)` — the spec's
  `participants[]` (BJJ: athlete + academy/team).
- `assets(id, video_id, kind, uri, codec, size_bytes, width, height, ts_ms,
  pipeline_version, created_at)`; kinds `original`, `mezzanine`, `proxy`,
  `keyframe`, `thumbnail`. Unique `(video_id, uri)`.
- `processing_jobs(id, video_id, stage, status, model_version, attempts,
  max_attempts, error, input, locked_until, created_at, updated_at,
  started_at, finished_at)`. Unique `(video_id, stage)`. Statuses `queued`,
  `running`, `succeeded`, `failed`, `skipped`.
- `upload_sessions(id, user_id, filename, size_bytes, received_bytes,
  metadata, status, video_id, created_at, updated_at)`.
- Indexes: `videos(content_hash)`, `assets(video_id, kind)`,
  `processing_jobs(status)`.

## 5. New dependencies

- `pytest` (dev only, `requirements-dev.txt`) — the repo had no test
  framework. No runtime dependencies added; ffmpeg still comes from
  `imageio-ffmpeg` (ffprobe is not bundled, so probing parses `ffmpeg -i`).

## 6. Pipeline design

Stages per video, in order: `ingest → probe → transcode → keyframes`. Each
is a `processing_jobs` row created at ingest; a stage is claimable once the
previous stage has succeeded. A worker claims a job with an atomic
`UPDATE … WHERE status='queued' OR lease expired` and keeps a lease alive
with a heartbeat thread, so a crashed/restarted process's job is retried
after its lease expires (safe with several gunicorn workers too). Failures
are retried automatically up to `JOB_MAX_ATTEMPTS` (default 3); after that
the job is `failed`, downstream stages become `skipped`, and the dashboard
offers a manual retry. The worker thread starts with the web app's first
request (not at import), so CLI commands such as `flask import-folder` never
spawn a worker that would die mid-job when the command exits.

- **ingest**: hash (SHA-256), exact-duplicate check, store the original
  write-once at `videos/<id>/original<ext>` (or adopt an object already in
  storage, for `/api/import` and storage-prefix bulk import). Small
  (≤ `COMPRESS_THRESHOLD_MB`) videos become playable immediately from the
  original, as before.
- **probe**: duration, fps, resolution, codec → `videos` + original asset.
- **transcode** (PRE-1): mezzanine H.264/AAC fit in 1920×1080, CRF 23, +
  480p proxy H.264 CRF 30; playback switches to the mezzanine. If ffmpeg
  cannot decode the file after all retries, the original stays playable (the
  previous behaviour).
- **keyframes** (PRE-3): from the proxy, frames at every scene change
  (ffmpeg scene score > `KEYFRAME_SCENE_THRESHOLD`, default 0.4) and at least
  every `KEYFRAME_INTERVAL_S` (default 10) seconds, 512 px wide; each stored
  as a `keyframe` asset with `ts_ms`. The poster thumbnail is produced here.

Derivative keys carry the pipeline version (`mezzanine-v1.mp4`,
`keyframes-v1/…`) so reprocessing with a new version (OPS-2) never needs to
overwrite. A retried stage adopts an object that already exists under its
key instead of overwriting it (objects are written atomically, so an
existing key is always complete).

## 7. Implementation steps (each tested on its own)

1. Test harness (`tests/conftest.py`, ffmpeg-generated sample videos).
   Storage: nested keys, write-once saves, `exists/size/list/download_to`
   (STO-1). Schema + migrations (STO-2).
2. ffmpeg primitives: `probe`, `transcode_renditions`, `extract_keyframes`
   (PRE-1, PRE-3).
3. Job queue + worker + stages; route the existing upload form through it
   with metadata/license fields (ING-4, PRE-1, PRE-3, ING-5 exact).
4. Resumable upload API + upload page JS (ING-1).
5. Bulk import: folder/manifest and storage-prefix CLI; `/api/import`
   registers into the pipeline (ING-2).
6. Job dashboard with retry (OPS-1).
7. Gate 1 end-to-end test (10 videos), README.

## 8. Assumptions

- **License is mandatory** on every ingestion path (spec Legal section).
  The website form pre-selects "own-footage"; `pipeline/sync_icloud.py` now
  sends `--license` (default `own-footage`). Older copies of the script that
  don't send a license get a 400 from `/api/import`.
- BJJ metadata: `competition` (e.g. "IBJJF Worlds 2026"), `event_date`,
  `participants` as "Name (Team)" entries separated by `;` or newlines.
  Division/ruleset are not in the spec; left for later.
- Job dashboard access: everyone logged in sees their own videos' jobs;
  usernames in `ADMIN_USERNAMES` see all and can retry anything (there is no
  admin role in the app yet).
- Resumable uploads stage chunks on the app's local disk (`DATA_DIR`), so
  the 20 GB limit needs that much free disk (Azure F1 has 1 GB, B1 10 GB).
  Streaming chunks straight into Azure blocks is a follow-up.
- All web uploads now finish asynchronously through the job pipeline; small
  ones are typically playable within seconds (previously instantly).
- Exact duplicates (same SHA-256) are kept as a row with status `duplicate`
  pointing at the first copy; they are hidden from the browse grid and get
  no derivatives. Perceptual near-duplicate flagging (rest of ING-5) is
  deferred.
- Gate 1 is proven with small ffmpeg-generated clips, not real match footage.

## 9. Open questions (non-blocking)

- Should duplicates be rejected outright instead of recorded?
- Which license vocabulary should be enforced (free text today)?
- Who are the admins in production (`ADMIN_USERNAMES`)?

## 10. Follow-ups (not built)

- Stream resumable chunks directly to Azure block blobs (no local staging).
- Perceptual-hash near-duplicate flagging (ING-5 remainder).
- PySceneDetect shot segments persisted as `segments` (PRE-2), audio (PRE-4).
- `AzureBlobStorage.save(overwrite=False)` relies on the service's
  if-none-match semantics; consider container immutability policies /
  versioning for the 11-nines/versioned NFR.
- Postgres port: swap `sqlite3` for psycopg, `?` → `%s`, TEXT JSON → JSONB,
  claim with `SELECT … FOR UPDATE SKIP LOCKED`.
- A JSON jobs API for agents (AGT-4) on top of `jobs.py`.

## 11. Traceability

| Req | Status | Code | Tests |
|---|---|---|---|
| ING-1 resumable upload ≤20 GB | **done** | `ingest.py` (resumable section), `app.py` `/uploads` routes, `static/upload.js`, `auth.py` (header CSRF) | `tests/test_resumable_upload.py`; browser run with injected network failure (manual, Playwright) |
| ING-2 bulk import (bucket/folder) | **done** (folder + local/Azure storage prefix; no S3/GCS) | `ingest.py` (bulk import), `app.py` CLI `import-folder`, `import-storage`, `run-jobs`; `/api/import` | `tests/test_bulk_import.py`, `tests/test_gate1.py` |
| ING-4 metadata on upload | **done** (license mandatory) | `ingest.validate_metadata`, `video_participants`, `templates/upload.html`, `templates/watch.html`, `pipeline/sync_icloud.py` | `tests/test_metadata.py`, `tests/test_pipeline.py`, `tests/test_bulk_import.py` |
| ING-5 dedupe (P1, optional) | **partial**: exact SHA-256 dedupe done; perceptual near-duplicates deferred | `stages.ingest` | `tests/test_pipeline.py::test_exact_duplicate_is_flagged_not_reprocessed` |
| PRE-1 mezzanine + 480p proxy | **done** | `processing.transcode_renditions`, `stages.transcode` | `tests/test_processing.py`, `tests/test_pipeline.py`, `tests/test_gate1.py` |
| PRE-3 keyframes (per shot + every N s) | **done** (shots via ffmpeg scene score; persisted shot segments are PRE-2, later) | `processing.extract_keyframes`, `stages.keyframes` | `tests/test_processing.py::test_keyframes_*`, `tests/test_gate1.py` |
| STO-1 immutable object storage | **done** (write-once keys on local disk and Azure; no versioning/immutability policy yet) | `storage.py`, `stages.put_once` | `tests/test_storage.py`, `tests/test_pipeline.py::test_original_is_byte_identical_and_immutable`, `::test_retried_stage_adopts_existing_objects`, `tests/test_remote_storage_pipeline.py` |
| STO-2 metadata in relational DB | **done** for Phase 0 entities (Video, Asset, ProcessingJob, participants); Segment/Event tables come with PRE-2/ENR | `db.py` | `tests/test_db.py`, all pipeline tests |
| OPS-1 job dashboard | **done** | `jobs.py` (queue, retries, leases, `dashboard`), `app.py` `/jobs`, `templates/jobs.html` | `tests/test_jobs.py`, `tests/test_dashboard.py` |
| Gate 1: 10 videos end to end | **passed** | — | `tests/test_gate1.py` |
| Existing features | **kept** | `app.py`, `processing.resume_pending` (legacy rows only) | `tests/test_existing_features.py` |
