"""PRE-1 (mezzanine + 480p proxy) and PRE-3 (keyframes) ffmpeg primitives."""
import pytest

import processing
from conftest import FFMPEG, make_video

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


def test_probe_reads_duration_fps_and_size(samples):
    info = processing.probe(FFMPEG, samples[0])
    assert info["codec"] == "h264"
    assert (info["width"], info["height"]) == (320, 240)
    assert info["fps"] == 15
    assert 1900 <= info["duration_ms"] <= 2100


def test_probe_rejects_non_video(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    with pytest.raises(processing.ProbeError):
        processing.probe(FFMPEG, str(junk))


def test_transcode_caps_mezzanine_at_1080p_and_proxy_at_480(tmp_path):
    src = make_video(tmp_path / "uhd.mp4", seconds=1, size="2560x1440")
    mezz, proxy = str(tmp_path / "m.mp4"), str(tmp_path / "p.mp4")
    processing.transcode_renditions(FFMPEG, src, mezz, proxy)
    m, p = processing.probe(FFMPEG, mezz), processing.probe(FFMPEG, proxy)
    assert (m["codec"], m["width"], m["height"]) == ("h264", 1920, 1080)
    assert (p["codec"], p["height"]) == ("h264", 480)
    assert p["width"] in (852, 854)  # 16:9 at 480 lines, even width


def test_transcode_never_upscales_and_handles_silent_video(tmp_path):
    src = make_video(tmp_path / "small.mp4", seconds=1, size="320x240", audio=False)
    mezz, proxy = str(tmp_path / "m.mp4"), str(tmp_path / "p.mp4")
    processing.transcode_renditions(FFMPEG, src, mezz, proxy)
    assert processing.probe(FFMPEG, mezz)["height"] == 240
    assert processing.probe(FFMPEG, proxy)["height"] == 240


def test_keyframes_every_n_seconds(tmp_path, samples):
    # samples[1] is 3 s long; with N=1 expect frames at ~0, 1, 2 s.
    frames = processing.extract_keyframes(FFMPEG, samples[1], str(tmp_path / "kf"),
                                          interval_s=1, scene_threshold=0.99)
    times = [t for t, _ in frames]
    assert times[0] == 0
    assert len(frames) == 3
    assert all(abs(t - 1000 * i) <= 100 for i, t in enumerate(times))


def test_keyframes_one_per_shot(tmp_path, scene_clip):
    # Hard cuts at 2 s and 4 s; a long interval so only shot changes count.
    frames = processing.extract_keyframes(FFMPEG, scene_clip, str(tmp_path / "kf"),
                                          interval_s=60, scene_threshold=0.4)
    times = [t for t, _ in frames]
    assert len(times) == 3
    assert times[0] == 0
    assert abs(times[1] - 2000) <= 100 and abs(times[2] - 4000) <= 100
    assert all(path.endswith(".jpg") for _, path in frames)
