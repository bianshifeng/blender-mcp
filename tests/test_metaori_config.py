"""MetaOri reads its ComfyUI address and output folders from the environment."""
from pathlib import Path

from blender_mcp.metaori.config import DEFAULT_COMFYUI_URL, MetaOriConfig


def test_defaults_live_under_the_home_directory(tmp_path):
    config = MetaOriConfig.from_env(env={}, cwd=tmp_path)
    assert config.comfyui_url == DEFAULT_COMFYUI_URL
    assert config.output_dir == (Path.home() / "MetaOri" / "AI_Raw").resolve()
    assert config.export_dir == (Path.home() / "MetaOri" / "Export").resolve()


def test_relative_dirs_resolve_against_the_working_directory(tmp_path):
    env = {"METAORI_OUTPUT_DIR": "SourceArt/AI_Raw", "METAORI_EXPORT_DIR": "SourceArt/Export"}
    config = MetaOriConfig.from_env(env=env, cwd=tmp_path)
    assert config.output_dir == (tmp_path / "SourceArt" / "AI_Raw").resolve()
    assert config.export_dir == (tmp_path / "SourceArt" / "Export").resolve()


def test_absolute_dirs_and_url_are_used_as_given(tmp_path):
    out = tmp_path / "out"
    env = {"METAORI_OUTPUT_DIR": str(out), "METAORI_COMFYUI_URL": "http://10.0.0.5:8188/"}
    config = MetaOriConfig.from_env(env=env, cwd=tmp_path / "elsewhere")
    assert config.output_dir == out.resolve()
    assert config.comfyui_url == "http://10.0.0.5:8188"


def test_blank_values_fall_back_to_defaults(tmp_path):
    config = MetaOriConfig.from_env(env={"METAORI_COMFYUI_URL": "  ", "METAORI_OUTPUT_DIR": ""}, cwd=tmp_path)
    assert config.comfyui_url == DEFAULT_COMFYUI_URL
    assert config.output_dir == (Path.home() / "MetaOri" / "AI_Raw").resolve()
