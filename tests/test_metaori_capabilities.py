"""Capability contracts validate standard parameters before anything reaches ComfyUI."""
import pytest

from blender_mcp.metaori.capabilities import (
    IMAGE_TO_3D,
    TEXT_TO_IMAGE,
    compose_prompt,
    load_presets,
    validate_asset_name,
)
from blender_mcp.metaori.errors import ParamError


def test_text_to_image_accepts_valid_params_and_drops_missing_ones():
    params = TEXT_TO_IMAGE.validate({"prompt": "crate", "width": 1024, "height": 768, "seed": 7, "count": None})
    assert params == {"prompt": "crate", "width": 1024, "height": 768, "seed": 7}


@pytest.mark.parametrize("params, message", [
    ({"width": 1000}, "multiple of 8"),
    ({"width": 128}, ">= 256"),
    ({"count": 5}, "<= 4"),
    ({"seed": -1}, ">= 0"),
    ({"seed": True}, "integer"),
    ({"prompt": 3}, "string"),
    ({"steps": 20}, "unknown"),
])
def test_text_to_image_rejects_bad_params(params, message):
    with pytest.raises(ParamError, match=message):
        TEXT_TO_IMAGE.validate(params)


def test_image_to_3d_checks_texture_size_and_flags():
    assert IMAGE_TO_3D.validate({"texture_size": 2048, "remove_background": False}) == {
        "texture_size": 2048, "remove_background": False}
    with pytest.raises(ParamError, match="one of"):
        IMAGE_TO_3D.validate({"texture_size": 3000})
    with pytest.raises(ParamError, match="true or false"):
        IMAGE_TO_3D.validate({"remove_background": "yes"})


def test_internal_inputs_are_marked():
    assert TEXT_TO_IMAGE.internal_inputs == ("filename_prefix",)
    assert IMAGE_TO_3D.internal_inputs == ("image", "filename_prefix")


@pytest.mark.parametrize("name", ["BronzeCenser", "WoodCrate_A", "Rock_01", "a"])
def test_asset_names_following_the_project_rule_pass(name):
    assert validate_asset_name(name) == name


@pytest.mark.parametrize("name", ["", "wood crate", "木箱", "1Crate", "Crate_", "Crate__A", "A" * 65, None])
def test_bad_asset_names_are_rejected(name):
    with pytest.raises(ParamError, match="asset_name"):
        validate_asset_name(name)


def test_presets_ship_reference_texture_and_raw():
    presets = load_presets()
    assert set(presets) == {"reference", "texture", "raw"}
    assert presets["reference"].width == 1024 and presets["reference"].height == 1024
    assert "white background" in presets["reference"].positive_suffix
    assert presets["raw"].positive_suffix == "" and presets["raw"].width is None


def test_compose_prompt_appends_the_preset_and_merges_negatives():
    preset = load_presets()["reference"]
    composed = compose_prompt(preset, "  bronze incense burner ", "rust")
    assert composed.positive == f"bronze incense burner, {preset.positive_suffix}"
    assert composed.negative == f"{preset.negative}, rust"


def test_raw_preset_keeps_the_prompt_and_the_workflow_negative():
    composed = compose_prompt(load_presets()["raw"], "a cat")
    assert composed.positive == "a cat"
    assert composed.negative is None


def test_empty_prompt_is_rejected():
    with pytest.raises(ParamError, match="empty"):
        compose_prompt(load_presets()["raw"], "   ")
