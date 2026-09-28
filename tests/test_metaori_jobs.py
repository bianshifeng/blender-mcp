"""Job folders are created once, never overwritten, and found again after a restart."""
import json

import pytest

from blender_mcp.metaori import jobs
from blender_mcp.metaori.errors import JobNotFound, MetaOriError
from blender_mcp.metaori.jobs import JOB_SCHEMA, JobStore, new_job_id


def test_create_makes_a_job_folder_with_a_record(tmp_path):
    store = JobStore(tmp_path)
    job_id = new_job_id()
    folder, record = store.create(job_id=job_id, kind="t2i", asset_name="Crate", record={"status": "queued"})
    assert folder.parent == tmp_path / "Crate"
    assert folder.name.endswith(f"_t2i_{job_id[:8]}")
    assert json.loads((folder / "job.json").read_text(encoding="utf-8")) == record
    assert record["schema"] == JOB_SCHEMA and record["job_id"] == job_id and record["status"] == "queued"
    assert record["folder"] == str(folder) and record["created_at"]


def test_existing_job_folders_are_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_timestamp", lambda: "20260928-120000")
    store = JobStore(tmp_path)
    job_id = new_job_id()
    store.create(job_id=job_id, kind="i2m", asset_name="Crate", record={})
    with pytest.raises(MetaOriError, match="refusing to overwrite"):
        store.create(job_id=job_id, kind="i2m", asset_name="Crate", record={})


def test_save_updates_the_record_atomically(tmp_path):
    store = JobStore(tmp_path)
    job_id = new_job_id()
    _, record = store.create(job_id=job_id, kind="t2i", asset_name="Crate", record={"status": "queued"})
    record["status"] = "succeeded"
    store.save(record)
    assert store.load(job_id)["status"] == "succeeded"
    assert not list(tmp_path.rglob("*.part"))


def test_jobs_are_found_again_by_a_fresh_store(tmp_path):
    job_id = new_job_id()
    JobStore(tmp_path).create(job_id=job_id, kind="t2i", asset_name="Crate", record={"status": "queued"})
    assert JobStore(tmp_path).load(job_id.upper())["job_id"] == job_id


def test_unknown_or_malformed_ids_raise_job_not_found(tmp_path):
    store = JobStore(tmp_path)
    with pytest.raises(JobNotFound, match="no MetaOri job"):
        store.load(new_job_id())
    with pytest.raises(JobNotFound, match="not a MetaOri job id"):
        store.load("../../etc/passwd")
