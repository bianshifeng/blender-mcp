"""Workflow packs bind MetaOri's standard parameters to nodes of an untouched ComfyUI graph."""
import json
from pathlib import Path

import pytest

from blender_mcp.metaori.errors import ParamError, WorkflowError
from blender_mcp.metaori.workflows import MANIFEST_SCHEMA, WorkflowRegistry, load_pack

GRAPH = {
    "1": {"class_type": "CR Prompt Text", "inputs": {"prompt": "x"}},
    "2": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "ComfyUI"}},
}


def _manifest(**overrides):
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "id": "t2i_test",
        "capability": "text_to_image",
        "inputs": {"prompt": [{"node": "1", "class_type": "CR Prompt Text", "input": "prompt"}]},
        "outputs": {"images": {"node": "3", "class_type": "SaveImage", "key": "images"}},
    }
    manifest.update(overrides)
    return manifest


def _write_pack(root: Path, manifest: dict, graph: dict = GRAPH, folder: str | None = None) -> Path:
    directory = root / (folder or manifest["id"])
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "workflow_api.json").write_text(json.dumps(graph), encoding="utf-8")
    return directory


def test_shipped_packs_load_as_the_defaults():
    registry = WorkflowRegistry.load()
    assert registry.errors == {}
    assert set(registry.packs) == {"t2i_qwen_image_2_1", "i2m_pixal3d"}
    assert registry.get("text_to_image").id == "t2i_qwen_image_2_1"
    assert registry.get("image_to_3d").id == "i2m_pixal3d"


def test_image_to_3d_binding_writes_every_target_and_leaves_the_pack_untouched():
    pack = WorkflowRegistry.load().get("image_to_3d")
    graph = pack.bind(
        {"image": "metaori/x.png", "seed": 5, "target_faces": 50000, "texture_size": 2048,
         "remove_background": False, "filename_prefix": "metaori/A/i2m_1234abcd"},
        {"model_variant": "trellis2", "remesh_resolution": 512},
    )
    assert graph["122"]["inputs"]["image"] == "metaori/x.png"
    assert [graph[n]["inputs"]["seed"] for n in ("3", "12", "18", "23")] == [5, 5, 5, 5]
    assert graph["186"]["inputs"]["target_face_count"] == 50000
    assert graph["288"]["inputs"]["value"] == 2048
    assert graph["248"]["inputs"]["switch"] is False
    assert graph["322"]["inputs"]["filename_prefix"] == "metaori/A/i2m_1234abcd"
    assert graph["316"]["inputs"]["value"] is True
    assert graph["241"]["inputs"]["resolution"] == 512
    assert pack.graph["3"]["inputs"]["seed"] == 56


def test_text_to_image_binding_replaces_links_with_constants():
    pack = WorkflowRegistry.load().get("text_to_image")
    assert pack.graph["456"]["inputs"]["width"] == ["13", 0]
    graph = pack.bind({"prompt": "crate", "width": 1024, "height": 512, "count": 2, "seed": 9})
    assert graph["502"]["inputs"]["prompt"] == "crate"
    assert graph["456"]["inputs"]["width"] == 1024
    assert graph["456"]["inputs"]["height"] == 512
    assert graph["456"]["inputs"]["batch_size"] == 2
    assert graph["458"]["inputs"]["seed"] == 9


def test_binding_rejects_unsupported_params_and_bad_extras():
    pack = WorkflowRegistry.load().get("image_to_3d")
    with pytest.raises(WorkflowError, match="does not support"):
        pack.bind({"image": "x.png", "width": 512})
    with pytest.raises(ParamError, match="no extra"):
        pack.bind({"image": "x.png"}, {"cfg": 3})
    with pytest.raises(ParamError, match="one of"):
        pack.bind({"image": "x.png"}, {"model_variant": "hunyuan"})
    with pytest.raises(ParamError, match="<= 2048"):
        pack.bind({"image": "x.png"}, {"remesh_resolution": 4096})


def test_describe_hides_internal_inputs_and_lists_extras():
    info = WorkflowRegistry.load().get("image_to_3d").describe()
    assert info["inputs"] == ["remove_background", "seed", "target_faces", "texture_size"]
    assert info["extras"]["model_variant"]["values"] == ["pixal3d", "trellis2"]
    assert info["extras"]["model_variant"]["default"] == "pixal3d"
    assert info["outputs"] == ["model"]


def test_resolve_extras_merges_caller_values_over_defaults():
    pack = WorkflowRegistry.load().get("image_to_3d")
    assert pack.resolve_extras({"model_variant": "trellis2"}) == {
        "model_variant": "trellis2", "remesh_resolution": 768}


def test_required_classes_cover_the_whole_graph():
    pack = WorkflowRegistry.load().get("image_to_3d")
    assert {"Save3DAdvanced", "LoadImage", "Pixal3DConditioning"} <= pack.required_classes()


@pytest.mark.parametrize("change, message", [
    ({"schema": "metaori.workflow/0"}, "schema"),
    ({"capability": "text_to_video"}, "unknown capability"),
    ({"inputs": {}}, "required inputs not bound"),
    ({"inputs": {"prompt": [{"node": "9", "class_type": "CR Prompt Text", "input": "prompt"}]}}, "not in graph"),
    ({"inputs": {"prompt": [{"node": "1", "class_type": "KSampler", "input": "prompt"}]}}, "is CR Prompt Text"),
    ({"inputs": {"prompt": [{"node": "1", "class_type": "CR Prompt Text", "input": "text"}]}}, "has no input"),
    ({"inputs": {"prompt": [{"node": "1", "class_type": "CR Prompt Text", "input": "prompt"}],
                 "steps": [{"node": "2", "class_type": "KSampler", "input": "steps"}]}}, "not a standard"),
    ({"outputs": {}}, "outputs not declared"),
    ({"extras": {"steps": {"type": "int", "default": 30,
                           "bind": [{"node": "2", "class_type": "KSampler", "input": "steps"}]}}}, "does not match"),
    ({"extras": {"steps": {"type": "number", "default": 20,
                           "bind": [{"node": "2", "class_type": "KSampler", "input": "steps"}]}}}, "needs a type"),
])
def test_invalid_manifests_are_rejected(tmp_path, change, message):
    directory = _write_pack(tmp_path, _manifest(**change))
    with pytest.raises(WorkflowError, match=message):
        load_pack(directory)


def test_manifest_id_must_match_its_folder(tmp_path):
    directory = _write_pack(tmp_path, _manifest(), folder="other_name")
    with pytest.raises(WorkflowError, match="must match the folder"):
        load_pack(directory)


def test_graph_must_be_api_format(tmp_path):
    directory = _write_pack(tmp_path, _manifest(), graph={"nodes": [], "links": []})
    with pytest.raises(WorkflowError, match="API-format"):
        load_pack(directory)


def test_registry_keeps_good_packs_and_reports_bad_ones(tmp_path):
    _write_pack(tmp_path, _manifest())
    _write_pack(tmp_path, _manifest(id="t2i_broken", outputs={}))
    registry = WorkflowRegistry.load(tmp_path)
    assert set(registry.packs) == {"t2i_test"}
    assert "outputs not declared" in registry.errors["t2i_broken"]
    with pytest.raises(WorkflowError, match="outputs not declared"):
        registry.get("text_to_image", "t2i_broken")


def test_registry_rejects_two_defaults_for_one_capability(tmp_path):
    _write_pack(tmp_path, _manifest(id="t2i_a", default=True))
    _write_pack(tmp_path, _manifest(id="t2i_b", default=True))
    registry = WorkflowRegistry.load(tmp_path)
    assert registry.packs == {}
    assert "more than one default" in registry.errors["t2i_a"]


def test_registry_get_explains_missing_or_mismatched_workflows(tmp_path):
    _write_pack(tmp_path, _manifest())
    registry = WorkflowRegistry.load(tmp_path)
    assert registry.get("text_to_image").id == "t2i_test"
    with pytest.raises(WorkflowError, match="no image_to_3d workflow"):
        registry.get("image_to_3d")
    with pytest.raises(WorkflowError, match="implements text_to_image"):
        registry.get("image_to_3d", "t2i_test")
    with pytest.raises(WorkflowError, match="not available"):
        registry.get("text_to_image", "nope")
