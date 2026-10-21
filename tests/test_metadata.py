"""ING-4: metadata attached on upload (competition, date, participants,
source, license), with license mandatory."""
import pytest

import ingest


def test_full_metadata_is_normalised():
    meta = ingest.validate_metadata({
        "title": "  Final  ", "competition": "IBJJF Worlds 2026",
        "event_date": "2026-06-01",
        "participants": "Ana Silva (Alliance)\nBea Costa (Checkmat)",
        "source": "Mat-side camera", "license": "own-footage",
    }, "final.mp4")
    assert meta["title"] == "Final"
    assert meta["event_date"] == "2026-06-01"
    assert meta["participants"] == [
        {"name": "Ana Silva", "team": "Alliance"},
        {"name": "Bea Costa", "team": "Checkmat"},
    ]


def test_license_is_required():
    with pytest.raises(ingest.MetadataError, match="license is required"):
        ingest.validate_metadata({"title": "x"}, "x.mp4")


def test_title_defaults_to_filename():
    assert ingest.validate_metadata({"license": "l"}, "dir/round_1.mp4")["title"] == "round_1"


@pytest.mark.parametrize("bad", ["2026-13-01", "01/06/2026", "yesterday"])
def test_bad_dates_rejected(bad):
    with pytest.raises(ingest.MetadataError, match="YYYY-MM-DD"):
        ingest.validate_metadata({"license": "l", "event_date": bad})


def test_participants_from_json_list_and_semicolons():
    assert ingest.parse_participants([{"name": "A", "team": "T"}, "B"]) == [
        {"name": "A", "team": "T"}, {"name": "B", "team": ""}]
    assert len(ingest.parse_participants("A (X); B (Y)")) == 2


def test_participant_limits():
    with pytest.raises(ingest.MetadataError):
        ingest.parse_participants(";".join(f"P{i}" for i in range(25)))
    with pytest.raises(ingest.MetadataError):
        ingest.parse_participants([{"name": "", "team": "T"}])


def test_upload_form_requires_license(logged_in, samples):
    client, _, csrf = logged_in
    with open(samples[0], "rb") as f:
        resp = client.post("/upload", data={
            "csrf_token": csrf, "video": (f, "a.mp4"), "license": "  ",
        }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert b"License is required" in resp.data
