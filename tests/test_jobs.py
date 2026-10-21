"""Job queue mechanics: ordering, retries, failures, leases, manual retry."""
import pytest

import db
import ingest
import jobs
import stages
from conftest import make_user, query


@pytest.fixture
def ctx(appmod):
    return appmod.pipeline


@pytest.fixture
def video(appmod, ctx):
    user_id = make_user(appmod, "jobber")
    conn = db.connect(ctx.database)
    vid = ingest.create_video(
        conn, user_id=user_id, meta=ingest.validate_metadata({"license": "own"}, "a.mp4"),
        filename="a.mp4", size_bytes=1, ingest_input={}, max_attempts=3)
    conn.commit()
    conn.close()
    return vid


@pytest.fixture
def fake_stages(monkeypatch):
    """Replace the real stages with recorders; set .fail[stage] = n to fail
    that stage's first n attempts (or 'always' / 'permanent')."""
    calls, fail = [], {}

    def make(stage):
        def fn(ctx, conn, video, job_input):
            calls.append(stage)
            mode = fail.get(stage)
            if mode == "permanent":
                raise jobs.PermanentError("bad input")
            if mode == "always" or (isinstance(mode, int) and calls.count(stage) <= mode):
                raise RuntimeError(f"{stage} boom")
        return fn

    monkeypatch.setattr(stages, "STAGE_FUNCTIONS", {s: make(s) for s in jobs.STAGES})
    return calls, fail


def statuses(appmod, vid):
    return dict(query(appmod, "SELECT stage, status FROM processing_jobs WHERE video_id = ?", (vid,)))


def test_stages_run_in_order(appmod, ctx, video, fake_stages):
    calls, _ = fake_stages
    assert jobs.run_pending(ctx) == 4
    assert calls == list(jobs.STAGES)
    assert set(statuses(appmod, video).values()) == {"succeeded"}
    versions = dict(query(appmod, "SELECT stage, model_version FROM processing_jobs"))
    assert versions["transcode"] == "transcode-v1"


def test_transient_failure_is_retried(appmod, ctx, video, fake_stages):
    calls, fail = fake_stages
    fail["probe"] = 2  # fails twice, succeeds on the 3rd (max) attempt
    jobs.run_pending(ctx)
    assert calls.count("probe") == 3
    row = query(appmod, "SELECT status, attempts, error FROM processing_jobs WHERE stage='probe'")[0]
    assert (row["status"], row["attempts"], row["error"]) == ("succeeded", 3, None)


def test_exhausted_retries_fail_and_skip_later_stages(appmod, ctx, video, fake_stages):
    calls, fail = fake_stages
    fail["transcode"] = "always"
    jobs.run_pending(ctx)
    assert calls.count("transcode") == 3
    assert "keyframes" not in calls
    s = statuses(appmod, video)
    assert (s["transcode"], s["keyframes"]) == ("failed", "skipped")
    err = query(appmod, "SELECT error FROM processing_jobs WHERE stage='transcode'")[0][0]
    assert "transcode boom" in err


def test_permanent_error_is_not_retried(appmod, ctx, video, fake_stages):
    calls, fail = fake_stages
    fail["ingest"] = "permanent"
    jobs.run_pending(ctx)
    assert calls == ["ingest"]
    # No original was stored, so the video itself has failed.
    assert query(appmod, "SELECT status FROM videos WHERE id = ?", (video,))[0][0] == "failed"


def test_manual_retry_requeues_failed_and_skipped(appmod, ctx, video, fake_stages):
    calls, fail = fake_stages
    fail["transcode"] = "always"
    jobs.run_pending(ctx)
    fail.clear()
    conn = db.connect(ctx.database)
    job_id = conn.execute("SELECT id FROM processing_jobs WHERE stage='transcode'").fetchone()[0]
    assert jobs.retry(conn, job_id) == video
    conn.commit()
    conn.close()
    jobs.run_pending(ctx)
    assert set(statuses(appmod, video).values()) == {"succeeded"}


def test_expired_lease_is_reclaimed(appmod, ctx, video, fake_stages):
    calls, _ = fake_stages
    # Simulate a worker that claimed ingest and then died.
    conn = db.connect(ctx.database)
    conn.execute("UPDATE processing_jobs SET status='running', attempts=1,"
                 " locked_until='2000-01-01 00:00:00' WHERE stage='ingest'")
    conn.commit()
    conn.close()
    jobs.run_pending(ctx)
    assert calls[0] == "ingest"
    assert statuses(appmod, video)["ingest"] == "succeeded"


def test_live_lease_is_not_stolen(appmod, ctx, video, fake_stages):
    conn = db.connect(ctx.database)
    conn.execute("UPDATE processing_jobs SET status='running', attempts=1,"
                 " locked_until='2999-01-01 00:00:00' WHERE stage='ingest'")
    conn.commit()
    conn.close()
    assert jobs.claim_next(ctx) is None


def test_lost_worker_on_last_attempt_fails_job(appmod, ctx, video, fake_stages):
    conn = db.connect(ctx.database)
    conn.execute("UPDATE processing_jobs SET status='running', attempts=3,"
                 " locked_until='2000-01-01 00:00:00' WHERE stage='ingest'")
    conn.commit()
    conn.close()
    jobs.run_pending(ctx)
    s = statuses(appmod, video)
    assert (s["ingest"], s["probe"]) == ("failed", "skipped")
    assert query(appmod, "SELECT status FROM videos WHERE id = ?", (video,))[0][0] == "failed"
