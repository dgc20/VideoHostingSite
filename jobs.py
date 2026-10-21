"""Processing job queue and background worker (stage 2 of the pipeline).

Each ingested video gets one `processing_jobs` row per stage, run in order:

    ingest -> probe -> transcode -> keyframes

The queue lives in the database, so it survives restarts and needs no extra
infrastructure. A worker claims a job with a single conditional UPDATE (only
one claimer can win) and holds a lease on it, renewed by a heartbeat while
the stage runs. If the process dies, the lease expires and another worker
(or this one after a restart) picks the job up again. This also keeps
several gunicorn workers from running the same job twice.

Failures are retried automatically up to the job's max_attempts; after that
the job is 'failed', later stages are 'skipped', and the job dashboard can
retry it. Like processing.py, work runs on a daemon thread in the web
process; the stage functions themselves live in stages.py.
"""
import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger(__name__)

STAGES = ("ingest", "probe", "transcode", "keyframes")

QUEUED, RUNNING, SUCCEEDED, FAILED, SKIPPED = (
    "queued", "running", "succeeded", "failed", "skipped",
)
TERMINAL = (SUCCEEDED, FAILED, SKIPPED)


class PermanentError(Exception):
    """A failure retrying can't fix (e.g. the file isn't a video)."""


class StopPipeline(Exception):
    """End this video's pipeline early without failing the current stage.

    The current job is marked `status` ('succeeded' or 'skipped') and every
    later stage is skipped with `reason` (e.g. an exact duplicate, or ffmpeg
    missing).
    """

    def __init__(self, reason, status=SUCCEEDED):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class PipelineContext:
    """Everything a stage needs, independent of Flask's request context."""

    database: str
    storage: object
    work_root: str
    compress_threshold: int = 500 * 1024 * 1024
    keyframe_interval_s: float = 10
    keyframe_scene_threshold: float = 0.4
    max_attempts: int = 3
    lease_s: int = 120
    extra: dict = field(default_factory=dict)


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _now():
    return datetime.now(timezone.utc)


# --- enqueueing -------------------------------------------------------------

def enqueue_pipeline(conn, video_id, ingest_input, max_attempts=3):
    """Create one queued job per stage for a new video. Caller commits."""
    now = db.utcnow(precise=True)  # created_at orders the queue FIFO
    for stage in STAGES:
        conn.execute(
            "INSERT INTO processing_jobs (id, video_id, stage, status,"
            " attempts, max_attempts, input, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (uuid.uuid4().hex, video_id, stage, QUEUED, max_attempts,
             json.dumps(ingest_input) if stage == "ingest" else None, now, now),
        )


def retry(conn, job_id):
    """Requeue a failed/skipped job and every unfinished stage after it.

    Returns the video_id, or None if the job doesn't exist or isn't
    retryable. Caller commits.
    """
    job = conn.execute(
        "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None or job["status"] not in (FAILED, SKIPPED):
        return None
    later = STAGES[STAGES.index(job["stage"]):]
    placeholders = ",".join("?" for _ in later)
    conn.execute(
        "UPDATE processing_jobs SET status = ?, attempts = 0, error = NULL,"
        " locked_until = NULL, finished_at = NULL, updated_at = ?"
        f" WHERE video_id = ? AND stage IN ({placeholders})"
        " AND status IN (?, ?)",
        (QUEUED, db.utcnow(), job["video_id"], *later, FAILED, SKIPPED),
    )
    # A video whose ingest failed is back to processing while it retries.
    conn.execute(
        "UPDATE videos SET status = 'processing' WHERE id = ? AND status = 'failed'",
        (job["video_id"],),
    )
    return job["video_id"]


# --- claiming ---------------------------------------------------------------

def _expire_lost(conn, now):
    """Fail running jobs whose worker vanished after their last attempt."""
    rows = conn.execute(
        "SELECT * FROM processing_jobs WHERE status = ? AND locked_until < ?"
        " AND attempts >= max_attempts",
        (RUNNING, _ts(now)),
    ).fetchall()
    for job in rows:
        _finish_failed(conn, job, "worker stopped while running (lease expired)")
    if rows:
        conn.commit()
    return [job["video_id"] for job in rows]


def claim_next(ctx):
    """Claim the next runnable job, or return None.

    Runnable: queued (or running with an expired lease) and the previous
    stage of the same video has succeeded.
    """
    conn = db.connect(ctx.database)
    try:
        now = _now()
        for video_id in _expire_lost(conn, now):
            _on_pipeline_end(ctx, conn, video_id)
        candidates = conn.execute(
            "SELECT * FROM processing_jobs"
            " WHERE status = ? OR (status = ? AND locked_until < ?)"
            " ORDER BY created_at, video_id",
            (QUEUED, RUNNING, _ts(now)),
        ).fetchall()
        for job in candidates:
            index = STAGES.index(job["stage"])
            if index > 0:
                prev = conn.execute(
                    "SELECT status FROM processing_jobs"
                    " WHERE video_id = ? AND stage = ?",
                    (job["video_id"], STAGES[index - 1]),
                ).fetchone()
                if prev is None or prev["status"] != SUCCEEDED:
                    continue
            lease = _ts(now + timedelta(seconds=ctx.lease_s))
            cur = conn.execute(
                "UPDATE processing_jobs SET status = ?, attempts = attempts + 1,"
                " locked_until = ?, started_at = ?, updated_at = ?"
                " WHERE id = ? AND (status = ? OR (status = ? AND locked_until < ?))",
                (RUNNING, lease, _ts(now), _ts(now), job["id"],
                 QUEUED, RUNNING, _ts(now)),
            )
            conn.commit()
            if cur.rowcount == 1:
                return conn.execute(
                    "SELECT * FROM processing_jobs WHERE id = ?", (job["id"],)
                ).fetchone()
        return None
    finally:
        conn.close()


# --- running ----------------------------------------------------------------

def _skip_later(conn, job, reason):
    later = STAGES[STAGES.index(job["stage"]) + 1:]
    if not later:
        return
    placeholders = ",".join("?" for _ in later)
    conn.execute(
        f"UPDATE processing_jobs SET status = ?, error = ?, updated_at = ?"
        f" WHERE video_id = ? AND stage IN ({placeholders})"
        " AND status NOT IN (?, ?)",
        (SKIPPED, reason, db.utcnow(), job["video_id"], *later, SUCCEEDED, RUNNING),
    )


def _finish_failed(conn, job, error):
    conn.execute(
        "UPDATE processing_jobs SET status = ?, error = ?, locked_until = NULL,"
        " finished_at = ?, updated_at = ? WHERE id = ?",
        (FAILED, error, db.utcnow(), db.utcnow(), job["id"]),
    )
    _skip_later(conn, job, f"skipped: {job['stage']} failed")


def _on_pipeline_end(ctx, conn, video_id):
    """Tidy up once a video has no queued/running stages left."""
    import stages

    pending = conn.execute(
        "SELECT 1 FROM processing_jobs WHERE video_id = ? AND status IN (?, ?)",
        (video_id, QUEUED, RUNNING),
    ).fetchone()
    if pending:
        return
    stages.on_pipeline_end(ctx, conn, video_id)
    conn.commit()
    shutil.rmtree(stages.work_dir(ctx, video_id), ignore_errors=True)


class _Heartbeat:
    """Extends a job's lease every lease_s/3 seconds while it runs."""

    def __init__(self, ctx, job_id):
        self.ctx, self.job_id = ctx, job_id
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        interval = max(1, self.ctx.lease_s / 3)
        while not self.stop.wait(interval):
            conn = db.connect(self.ctx.database)
            try:
                conn.execute(
                    "UPDATE processing_jobs SET locked_until = ?"
                    " WHERE id = ? AND status = ?",
                    (_ts(_now() + timedelta(seconds=self.ctx.lease_s)),
                     self.job_id, RUNNING),
                )
                conn.commit()
            except Exception:
                log.exception("heartbeat failed for job %s", self.job_id)
            finally:
                conn.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


def run_job(ctx, job):
    """Run one claimed job and record the outcome."""
    import stages

    stage_fn = stages.STAGE_FUNCTIONS[job["stage"]]
    version = stages.STAGE_VERSIONS.get(job["stage"])
    job_input = json.loads(job["input"]) if job["input"] else {}
    conn = db.connect(ctx.database)
    try:
        video = conn.execute(
            "SELECT * FROM videos WHERE id = ?", (job["video_id"],)
        ).fetchone()
        if video is None:
            # Deleted while queued; its job rows are normally deleted too.
            conn.execute("DELETE FROM processing_jobs WHERE id = ?", (job["id"],))
            conn.commit()
            return
        try:
            with _Heartbeat(ctx, job["id"]):
                stage_fn(ctx, conn, video, job_input)
        except StopPipeline as stop:
            # Not rolled back: the stage's own updates (e.g. marking a
            # duplicate) are part of the outcome.
            conn.execute(
                "UPDATE processing_jobs SET status = ?, error = ?, model_version = ?,"
                " locked_until = NULL, finished_at = ?, updated_at = ? WHERE id = ?",
                (stop.status, stop.reason if stop.status != SUCCEEDED else None,
                 version, db.utcnow(), db.utcnow(), job["id"]),
            )
            _skip_later(conn, job, f"skipped: {stop.reason}")
            log.info("pipeline for %s stopped at %s: %s",
                     job["video_id"], job["stage"], stop.reason)
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"[:2000]
            permanent = isinstance(exc, PermanentError)
            if permanent or job["attempts"] >= job["max_attempts"]:
                log.warning("job %s (%s) failed: %s", job["id"], job["stage"], error)
                _finish_failed(conn, job, error)
            else:
                log.info("job %s (%s) attempt %d failed, will retry: %s",
                         job["id"], job["stage"], job["attempts"], error)
                conn.execute(
                    "UPDATE processing_jobs SET status = ?, error = ?,"
                    " locked_until = NULL, updated_at = ? WHERE id = ?",
                    (QUEUED, error, db.utcnow(), job["id"]),
                )
        else:
            conn.execute(
                "UPDATE processing_jobs SET status = ?, error = NULL,"
                " model_version = ?, locked_until = NULL, finished_at = ?,"
                " updated_at = ? WHERE id = ?",
                (SUCCEEDED, version, db.utcnow(), db.utcnow(), job["id"]),
            )
        conn.commit()
        still_there = conn.execute(
            "SELECT 1 FROM videos WHERE id = ?", (job["video_id"],)
        ).fetchone()
        if still_there is None:
            # Deleted while this stage ran: drop whatever it just stored.
            for key in ctx.storage.list(f"videos/{job['video_id']}/"):
                ctx.storage.delete(key)
            conn.execute(
                "DELETE FROM processing_jobs WHERE video_id = ?", (job["video_id"],)
            )
            conn.execute("DELETE FROM assets WHERE video_id = ?", (job["video_id"],))
            conn.commit()
            shutil.rmtree(stages.work_dir(ctx, job["video_id"]), ignore_errors=True)
            return
        _on_pipeline_end(ctx, conn, job["video_id"])
    finally:
        conn.close()


def run_pending(ctx, max_jobs=None):
    """Run jobs synchronously until none are runnable. Returns the count.

    Used by the `flask run-jobs` command and by tests.
    """
    count = 0
    while max_jobs is None or count < max_jobs:
        job = claim_next(ctx)
        if job is None:
            break
        run_job(ctx, job)
        count += 1
    return count


# --- background worker -------------------------------------------------------

class Worker(threading.Thread):
    """Daemon thread that drains the queue, waking on notify() or a poll."""

    def __init__(self, ctx, poll_s=5):
        super().__init__(daemon=True, name="pipeline-worker")
        self.ctx, self.poll_s = ctx, poll_s
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            try:
                ran = run_pending(self.ctx)
            except Exception:
                log.exception("pipeline worker error")
                ran = 0
            if not ran:
                self.wake.wait(self.poll_s)
                self.wake.clear()

    def stop(self):
        self.stopping.set()
        self.wake.set()


_worker = None


def start_worker(ctx, poll_s=5):
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = Worker(ctx, poll_s)
        _worker.start()
    return _worker


def stop_worker():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker.join(timeout=10)
        _worker = None


def notify():
    """Wake the background worker (if running) after enqueueing work."""
    if _worker is not None:
        _worker.wake.set()


def wait_idle(ctx, timeout=60):
    """Block until no job is queued or running (tests, CLI)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = db.connect(ctx.database)
        try:
            busy = conn.execute(
                "SELECT 1 FROM processing_jobs WHERE status IN (?, ?)",
                (QUEUED, RUNNING),
            ).fetchone()
        finally:
            conn.close()
        if not busy:
            return True
        time.sleep(0.2)
    return False
