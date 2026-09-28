"""The bpy scripts run in a real Blender in background mode; skipped when Blender is not installed.

Set METAORI_BLENDER_EXE when blender(.exe) is neither on PATH nor in the default location.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from blender_mcp.metaori.blender_ops import build_code, parse_output

DEFAULT_BLENDER = r"D:\Program Files\Blender Foundation\Blender 5.1\blender.exe"


def _find_blender():
    for candidate in (os.environ.get("METAORI_BLENDER_EXE"), shutil.which("blender"), DEFAULT_BLENDER):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


BLENDER = _find_blender()
pytestmark = pytest.mark.skipif(BLENDER is None, reason="Blender not found; set METAORI_BLENDER_EXE")

# A generated-model stand-in: two meshes under an empty, flat shaded (so glTF splits every
# vertex), rotated, scaled and moved, with a tiny floating chip that normalization must drop.
SETUP = """
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
root = bpy.data.objects.new("TestProp", None)
bpy.context.scene.collection.objects.link(root)
bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.5)
body = bpy.context.active_object
body.name = "Body"
material = bpy.data.materials.new("RandomMat")
body.data.materials.append(material)
bpy.ops.mesh.primitive_cube_add(size=0.05, location=(2.0, 0.0, 0.0))
chip = bpy.context.active_object
chip.name = "Chip"
chip.data.materials.append(material)
for part in (body, chip):
    part.parent = root
root.location = (3.0, 4.0, 5.0)
root.rotation_euler = (1.5707963, 0.0, 0.0)
root.scale = (2.0, 2.0, 2.0)
bpy.ops.export_scene.gltf(filepath=GLB, export_format="GLB")
bpy.ops.wm.read_factory_settings(use_empty=True)
print("SETUP_DONE")
"""


def _run_in_blender(tmp_path, *parts):
    script = tmp_path / "run.py"
    script.write_text("\n\n".join(parts), encoding="utf-8")
    proc = subprocess.run(
        [BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    assert "SETUP_DONE" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-3000:]
    return [line for line in proc.stdout.splitlines() if line.startswith(("METAORI_RESULT:", "METAORI_ERROR:"))]


def test_import_and_normalize_a_generated_style_model(tmp_path):
    glb = tmp_path / "prop.glb"
    fbx = tmp_path / "export" / "TestProp" / "SM_TestProp.fbx"
    normalize_params = {
        "object_name": "TestProp", "asset_name": "TestProp", "apply_modifiers": True, "rotate_deg": None,
        "target_size_m": 0.5, "size_axis": "height", "merge_distance_m": 0.0001, "min_island_ratio": 0.01,
        "recalc_normals": True, "target_tris": 1500, "smooth_angle_deg": 30.0, "rename": True,
        "export_path": str(fbx), "overwrite_export": False,
    }
    lines = _run_in_blender(
        tmp_path,
        f"GLB = {str(glb)!r}\n" + SETUP,
        build_code("import_model", {"file_path": str(glb), "name": None}),
        build_code("normalize", normalize_params),
    )
    assert len(lines) == 2, lines
    imported = parse_output("import_model", lines[0])
    report = parse_output("normalize", lines[1])

    assert imported["roots"] == ["TestProp"]
    assert sorted(imported["mesh_objects"]) == ["Body", "Chip"]
    assert imported["world_bbox"]["dimensions"][2] == pytest.approx(2.0, abs=0.01)
    assert imported["tris"] > 3900

    after = report["after"]
    assert report["object"] == "SM_TestProp"
    assert after["materials"] == ["MI_TestProp"]
    assert after["dimensions_m"][2] == pytest.approx(0.5, abs=0.005)
    assert after["bbox_min"][2] == pytest.approx(0.0, abs=1e-4)
    assert (after["bbox_min"][0] + after["bbox_max"][0]) / 2 == pytest.approx(0.0, abs=1e-3)
    assert (after["bbox_min"][1] + after["bbox_max"][1]) / 2 == pytest.approx(0.0, abs=1e-3)
    assert after["location"] == [0.0, 0.0, 0.0]
    assert after["islands"] == 1
    assert 1000 <= after["tris"] <= 1600
    assert after["non_manifold_edges"] == 0
    assert after["custom_normals"] is False
    steps = {step["step"]: step for step in report["steps"]}
    assert steps["join"]["objects"] == 2
    assert steps["islands"]["removed"] == 1
    assert steps["merge_and_loose"]["merged_verts"] > 0
    assert fbx.is_file() and fbx.stat().st_size > 1000
    assert report["export"]["path"] == str(fbx)
    assert any("TestProp" in warning for warning in report["warnings"])
