"""MetaOriService drives jobs from validation to downloaded outputs against a fake ComfyUI."""
import asyncio
import json
from pathlib import Path

import pytest

from blender_mcp.metaori.errors import ComfyRequestError, ParamError, WorkflowError
from metaori_fakes import make_service

T2I_PREFIX = "metaori/BronzeCenser"


def run(coro):
    return asyncio.run(coro)


def _image_job(service, comfy, **kwargs):
    record = run(service.generate_image(prompt="bronze censer", asset_name="BronzeCenser", **kwargs))
    return record, dict(comfy.submitted)[record["job_id"]]


def _finished_image_job(service, comfy):
    record, _ = _image_job(service, comfy)
    comfy.succeed(record["job_id"], "461", "images", [(T2I_PREFIX, "a.png", b"\x89PNG")])
    return run(service.get_job(record["job_id"]))


def test_generate_image_submits_the_bound_graph_and_records_the_job(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, graph = _image_job(service, comfy)
    job8 = record["job_id"][:8]
    assert record["status"] == "queued" and record["queue_position"] == 1
    assert graph["502"]["inputs"]["prompt"].startswith("bronze censer, single object")
    assert (graph["456"]["inputs"]["width"], graph["456"]["inputs"]["height"]) == (1024, 1024)
    assert graph["458"]["inputs"]["seed"] == record["params"]["seed"]
    assert graph["461"]["inputs"]["filename_prefix"] == f"{T2I_PREFIX}/t2i_{job8}"
    assert graph["452"]["inputs"]["negative_prompt"] == service.presets["reference"].negative
    assert record["prompt"]["user"] == "bronze censer"
    assert record["workflow"]["id"] == "t2i_qwen_image_2_1" and len(record["workflow"]["sha256"]) == 64
    folder = Path(record["folder"])
    assert folder.parent == tmp_path / "out" / "BronzeCenser" and folder.name.endswith(f"_t2i_{job8}")
    assert json.loads((folder / "job.json").read_text(encoding="utf-8"))["status"] == "queued"


def test_successful_job_downloads_its_outputs_once(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy, count=2)
    comfy.succeed(record["job_id"], "461", "images",
                  [(T2I_PREFIX, "a_00001_.png", b"one"), (T2I_PREFIX, "a_00002_.png", b"two")])
    done = run(service.get_job(record["job_id"]))
    folder = Path(record["folder"])
    assert done["status"] == "succeeded"
    assert done["outputs"]["images"] == [str(folder / "image_01.png"), str(folder / "image_02.png")]
    assert (folder / "image_02.png").read_bytes() == b"two"
    assert done["execution_s"] == 12.5 and done["finished_at"]
    run(service.get_job(record["job_id"]))
    assert len(comfy.downloads) == 2


def test_running_and_queued_jobs_report_progress(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    first, _ = _image_job(service, comfy)
    second, _ = _image_job(service, comfy)
    assert second["queue_position"] == 2
    comfy.start(first["job_id"])
    running = run(service.get_job(first["job_id"]))
    assert running["status"] == "running" and running["started_at"]
    assert run(service.get_job(second["job_id"]))["queue_position"] == 1


def test_get_job_waits_until_the_job_finishes(tmp_path):
    service, comfy, clock = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    clock.on_sleep = lambda: comfy.succeed(record["job_id"], "461", "images", [(T2I_PREFIX, "a.png", b"x")])
    assert run(service.get_job(record["job_id"], wait_seconds=30))["status"] == "succeeded"
    assert clock.sleeps == [2.0]


def test_get_job_returns_the_current_state_when_the_wait_runs_out(tmp_path):
    service, comfy, clock = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    assert run(service.get_job(record["job_id"], wait_seconds=5))["status"] == "queued"
    assert sum(clock.sleeps) == pytest.approx(5.0)


def test_execution_errors_fail_the_job_with_node_details(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    comfy.fail(record["job_id"])
    failed = run(service.get_job(record["job_id"]))
    assert failed["status"] == "failed"
    assert failed["error"]["node_id"] == "458" and "out of memory" in failed["error"]["message"]


def test_interrupted_jobs_are_cancelled(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    comfy.interrupt_history(record["job_id"])
    assert run(service.get_job(record["job_id"]))["status"] == "cancelled"


def test_jobs_comfyui_forgot_are_failed_as_lost(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    comfy.pending.clear()
    lost = run(service.get_job(record["job_id"]))
    assert lost["status"] == "failed" and lost["error"]["reason"] == "lost"


def test_missing_outputs_fail_the_job(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    comfy.succeed(record["job_id"], "999", "images", [])
    failed = run(service.get_job(record["job_id"]))
    assert failed["status"] == "failed" and "returned no 'images'" in failed["error"]["message"]


def test_rejected_submission_is_recorded_as_failed(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    comfy.submit_error = ComfyRequestError("ComfyUI rejected the workflow: bad", details={"node_errors": {"502": {}}})
    record = run(service.generate_image(prompt="x", asset_name="Crate"))
    assert record["status"] == "failed"
    assert "bad" in record["error"]["message"] and "502" in record["error"]["node_errors"]


@pytest.mark.parametrize("kwargs, error", [
    ({"asset_name": "bad name"}, ParamError),
    ({"width": 1001}, ParamError),
    ({"purpose": "poster"}, ParamError),
    ({"params": {"nope": 1}}, ParamError),
    ({"workflow": "i2m_pixal3d"}, WorkflowError),
])
def test_invalid_requests_fail_before_any_job_folder_exists(tmp_path, kwargs, error):
    service, comfy, _ = make_service(tmp_path)
    with pytest.raises(error):
        run(service.generate_image(**{"prompt": "x", "asset_name": "Crate", **kwargs}))
    assert not (tmp_path / "out").exists() and comfy.submitted == []


def test_explicit_params_the_workflow_lacks_are_rejected_but_defaults_are_skipped(tmp_path, monkeypatch):
    service, comfy, _ = make_service(tmp_path)
    monkeypatch.delitem(service.registry.packs["t2i_qwen_image_2_1"].inputs, "width")
    with pytest.raises(WorkflowError, match="does not support"):
        run(service.generate_image(prompt="x", asset_name="Crate", width=512))
    record = run(service.generate_image(prompt="x", asset_name="Crate"))
    assert "width" in record["skipped_params"] and "width" not in record["params"]


def test_generate_model_chains_from_an_image_job(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    image_job = _finished_image_job(service, comfy)
    record = run(service.generate_model(image_job_id=image_job["job_id"], target_faces=50000, texture_size=2048))
    graph = dict(comfy.submitted)[record["job_id"]]
    assert comfy.uploads == [Path(image_job["outputs"]["images"][0])]
    assert graph["122"]["inputs"]["image"] == "metaori/image_01.png"
    assert graph["186"]["inputs"]["target_face_count"] == 50000
    assert graph["288"]["inputs"]["value"] == 2048
    assert len({graph[n]["inputs"]["seed"] for n in ("3", "12", "18", "23")}) == 1
    assert record["asset_name"] == "BronzeCenser"
    assert record["source"]["image_job_id"] == image_job["job_id"]
    assert (Path(record["folder"]) / "input.png").read_bytes() == b"\x89PNG"
    assert Path(record["folder"]).name.endswith(f"_i2m_{record['job_id'][:8]}")


def test_generate_model_downloads_the_glb(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    image = tmp_path / "ref.png"
    image.write_bytes(b"\x89PNG")
    record = run(service.generate_model(asset_name="Crate", image_path=str(image)))
    comfy.succeed(record["job_id"], "322", "3d", [("metaori/Crate", "i2m_00001.glb", b"glTF")])
    done = run(service.get_job(record["job_id"]))
    assert done["outputs"]["model"] == str(Path(record["folder"]) / "model.glb")


@pytest.mark.parametrize("kwargs, message", [
    ({}, "exactly one"),
    ({"image_path": "a.png", "image_job_id": "b"}, "exactly one"),
    ({"image_path": "missing.png", "asset_name": "Crate"}, "not found"),
    ({"image_path": "notes.txt", "asset_name": "Crate"}, "must be one of"),
])
def test_generate_model_checks_its_image_source(tmp_path, kwargs, message):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ParamError, match=message):
        run(service.generate_model(**kwargs))


def test_generate_model_needs_a_finished_image_job(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    with pytest.raises(ParamError, match="wait until it has succeeded"):
        run(service.generate_model(image_job_id=record["job_id"]))


def test_cancel_removes_a_pending_job(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    cancelled = run(service.cancel_job(record["job_id"]))
    assert cancelled["status"] == "cancelled" and comfy.deleted == [record["job_id"]]


def test_cancel_interrupts_a_running_job(tmp_path):
    service, comfy, clock = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    comfy.start(record["job_id"])
    clock.on_sleep = lambda: comfy.interrupt_history(record["job_id"])
    cancelled = run(service.cancel_job(record["job_id"]))
    assert cancelled["status"] == "cancelled" and comfy.interrupted == [record["job_id"]]


def test_status_reports_comfyui_workflows_and_missing_node_classes(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    classes = set().union(*(pack.required_classes() for pack in service.registry.packs.values()))
    comfy.object_info_classes = classes - {"CR Prompt Text"}
    status = run(service.status())
    assert status["comfyui"]["reachable"] and status["comfyui"]["version"] == "0.37.0"
    assert status["comfyui"]["devices"][0]["vram_total_gb"] == 24.0
    workflows = {w["id"]: w for w in status["workflows"]}
    assert workflows["t2i_qwen_image_2_1"]["missing_node_classes"] == ["CR Prompt Text"]
    assert workflows["t2i_qwen_image_2_1"]["available"] is False
    assert workflows["i2m_pixal3d"]["available"] is True
    assert status["output_dir"] == str(tmp_path / "out")
    assert set(status["purposes"]) == {"reference", "texture", "concept", "raw"}


def test_status_still_lists_workflows_when_comfyui_is_down(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    comfy.reachable = False
    status = run(service.status())
    assert status["comfyui"]["reachable"] is False and "not reachable" in status["comfyui"]["error"]
    assert {w["id"] for w in status["workflows"]} == {"t2i_qwen_image_2_1", "t2i_qwen_image_2_1_ref", "i2m_pixal3d"}
    assert "available" not in status["workflows"][0]


def test_jobs_survive_a_service_restart(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    record, _ = _image_job(service, comfy)
    restarted, _, _ = make_service(tmp_path, comfy=comfy)
    assert run(restarted.get_job(record["job_id"]))["status"] == "queued"


def _reference_files(tmp_path, count):
    paths = []
    for index in range(count):
        path = tmp_path / f"ref{index}.png"
        path.write_bytes(b"PNG" + bytes([index + 1]))
        paths.append(str(path))
    return paths


def test_reference_images_are_uploaded_copied_and_pick_the_reference_workflow(tmp_path):
    service, comfy, _ = make_service(tmp_path)
    refs = _reference_files(tmp_path, 2)
    record = run(service.generate_image(prompt="crate in the style of image 1", asset_name="Crate",
                                        purpose="raw", reference_images=refs, params={"steps": 30}))
    graph = dict(comfy.submitted)[record["job_id"]]
    assert record["workflow"]["id"] == "t2i_qwen_image_2_1_ref"
    assert comfy.uploads == [Path(p) for p in refs]
    assert record["params"]["reference_images"] == ["metaori/ref0.png", "metaori/ref1.png"]
    assert graph["105"]["inputs"]["image"] == "metaori/ref0.png" and "107" not in graph
    assert graph["7"]["inputs"]["steps"] == 30
    assert graph["9"]["inputs"]["filename_prefix"] == f"metaori/Crate/t2i_{record['job_id'][:8]}"
    folder = Path(record["folder"])
    assert (folder / "reference_02.png").read_bytes() == b"PNG" + bytes([2])
    assert [s["copied_as"] for s in record["source"]["reference_images"]] == ["reference_01.png", "reference_02.png"]


@pytest.mark.parametrize("kwargs, error, message", [
    ({"reference_images": "one.png"}, ParamError, "list of strings"),
    ({"reference_images": ["missing.png"]}, ParamError, "not found"),
    ({"workflow": "t2i_qwen_image_2_1"}, WorkflowError, "does not support"),
    ({"params": {"lora_1_strength": 5}}, ParamError, "<= 2.0"),
    ({"count_refs": 7}, ParamError, "at most 6"),
])
def test_bad_reference_requests_fail_before_upload(tmp_path, kwargs, error, message):
    service, comfy, _ = make_service(tmp_path)
    kwargs = {"reference_images": _reference_files(tmp_path, kwargs.pop("count_refs", 1)), **kwargs}
    with pytest.raises(error, match=message):
        run(service.generate_image(prompt="x", asset_name="Crate", **kwargs))
    assert comfy.uploads == [] and comfy.submitted == [] and not (tmp_path / "out").exists()
