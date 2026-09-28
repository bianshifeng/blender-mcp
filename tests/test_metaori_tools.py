"""The metaori_* MCP tools wrap MetaOriService and the bpy scripts and answer in JSON."""
import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP

from blender_mcp.metaori import tools as metaori_tools
from blender_mcp.metaori.errors import ParamError
from metaori_fakes import make_service

TOOL_NAMES = {"metaori_status", "metaori_generate_image", "metaori_generate_model", "metaori_get_job",
              "metaori_cancel_job", "metaori_import_model", "metaori_normalize_model"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    service, comfy, _ = make_service(tmp_path)
    calls = []

    def fake_run_script(blender, script, params):
        calls.append((blender, script, params))
        return {"script": script, "ok": True}

    monkeypatch.setattr(metaori_tools, "run_script", fake_run_script)
    blender = object()
    tools = metaori_tools.register_tools(FastMCP("test"), lambda: blender, service)
    return {"tools": tools, "calls": calls, "blender": blender, "tmp": tmp_path}


def test_the_server_registers_every_metaori_tool():
    from blender_mcp.server import SERVER_INSTRUCTIONS, mcp

    assert TOOL_NAMES <= {tool.name for tool in run(mcp.list_tools())}
    assert "metaori_status" in SERVER_INSTRUCTIONS and len(SERVER_INSTRUCTIONS) < 2500


def test_generate_image_answers_with_the_job_record(env):
    record = json.loads(run(env["tools"]["metaori_generate_image"](prompt="bronze censer", asset_name="BronzeCenser")))
    assert record["status"] == "queued" and record["asset_name"] == "BronzeCenser"
    polled = json.loads(run(env["tools"]["metaori_get_job"](record["job_id"], wait_seconds=0)))
    assert polled["job_id"] == record["job_id"]


def test_import_model_from_a_file_runs_the_import_script(env):
    model = env["tmp"] / "prop.glb"
    model.write_bytes(b"glTF")
    result = json.loads(run(env["tools"]["metaori_import_model"](file_path=str(model), name="Prop")))
    blender, script, params = env["calls"][0]
    assert result == {"script": "import_model", "ok": True}
    assert blender is env["blender"] and script == "import_model"
    assert params == {"file_path": str(model.resolve()), "name": "Prop"}


def test_import_model_needs_a_finished_model_job(env):
    record = json.loads(run(env["tools"]["metaori_generate_image"](prompt="x", asset_name="Crate")))
    with pytest.raises(ParamError, match="has no model"):
        run(env["tools"]["metaori_import_model"](job_id=record["job_id"]))


@pytest.mark.parametrize("kwargs, message", [
    ({}, "exactly one"),
    ({"file_path": "a.blend"}, "must be one of"),
    ({"file_path": "missing.glb"}, "not found"),
])
def test_import_model_checks_its_source(env, kwargs, message):
    with pytest.raises(ParamError, match=message):
        run(env["tools"]["metaori_import_model"](**kwargs))


def test_normalize_sends_the_export_path_and_options(env):
    result = json.loads(run(env["tools"]["metaori_normalize_model"](
        object_name="geometry_0", asset_name="BronzeCenser", target_size_m=0.35, target_tris=50000,
        rotate_deg=[0, 0, 90])))
    assert result["script"] == "normalize"
    params = env["calls"][0][2]
    assert params["export_path"] == str(env["tmp"] / "export" / "BronzeCenser" / "SM_BronzeCenser.fbx")
    assert params["target_size_m"] == 0.35 and params["target_tris"] == 50000 and params["rotate_deg"] == [0, 0, 90]
    assert params["overwrite_export"] is False and params["smooth_angle_deg"] == 30.0


def test_normalize_without_export_sends_no_path(env):
    run(env["tools"]["metaori_normalize_model"](object_name="o", asset_name="Crate", export_fbx=False))
    assert env["calls"][0][2]["export_path"] is None


def test_normalize_refuses_to_overwrite_an_existing_export(env):
    existing = env["tmp"] / "export" / "Crate" / "SM_Crate.fbx"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"fbx")
    with pytest.raises(ParamError, match="overwrite_export"):
        run(env["tools"]["metaori_normalize_model"](object_name="o", asset_name="Crate"))
    run(env["tools"]["metaori_normalize_model"](object_name="o", asset_name="Crate", overwrite_export=True))
    assert env["calls"][0][2]["overwrite_export"] is True


@pytest.mark.parametrize("kwargs, message", [
    ({"asset_name": "bad name"}, "asset_name"),
    ({"size_axis": "diagonal"}, "size_axis"),
    ({"target_size_m": 0}, "target_size_m"),
    ({"rotate_deg": [90, 0]}, "rotate_deg"),
    ({"target_tris": 2}, "target_tris"),
    ({"merge_distance_m": -1}, "merge_distance_m"),
    ({"min_island_ratio": 1.5}, "min_island_ratio"),
])
def test_normalize_validates_arguments(env, kwargs, message):
    with pytest.raises(ParamError, match=message):
        run(env["tools"]["metaori_normalize_model"](**{"object_name": "o", "asset_name": "Crate", **kwargs}))
