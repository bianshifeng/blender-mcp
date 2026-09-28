"""import_model - MetaOri bpy script: import a model file into the open Blender scene.

Runs inside Blender. MetaOri sends this file plus a runner (blender_ops.build_code) through the
addon's execute_code command, and run(params) returns a JSON-serialisable dict.
Offline: blender -b scene.blend -P import_model.py -- --params params.json

params: file_path (absolute .glb/.gltf/.fbx/.obj), name (optional new name for a single root).
"""
import json
import os
import sys

import bpy
import numpy as np
from mathutils import Vector


def _supported(operator, wanted):
    """Keep only the keyword arguments this Blender version's operator accepts."""
    names = set(operator.get_rna_type().properties.keys())
    return {key: value for key, value in wanted.items() if key in names}


def _import(path):
    extension = os.path.splitext(path)[1].lower()
    if extension in (".glb", ".gltf"):
        operator = bpy.ops.import_scene.gltf
        # merge_vertices re-joins the vertices glTF splits along UV seams.
        operator(**_supported(operator, {"filepath": path, "merge_vertices": True,
                                         "import_scene_as_collection": False}))
    elif extension == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif extension == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError(f"unsupported model format {extension!r}; use .glb, .gltf, .fbx or .obj")


def _tri_count(mesh):
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return int((totals - 2).sum())


def _world_bbox(objects):
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    if not corners:
        return None
    low = [min(corner[i] for corner in corners) for i in range(3)]
    high = [max(corner[i] for corner in corners) for i in range(3)]
    return {"min": [round(v, 5) for v in low], "max": [round(v, 5) for v in high],
            "dimensions": [round(high[i] - low[i], 5) for i in range(3)]}


def _images(materials):
    found = {}
    for material in materials:
        if material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                found[node.image.name] = list(node.image.size)
    return [{"name": name, "size": size} for name, size in sorted(found.items())]


def run(params):
    path = params["file_path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    before = set(bpy.data.objects)
    _import(path)
    bpy.context.view_layer.update()
    created = [obj for obj in bpy.data.objects if obj not in before]
    if not created:
        raise RuntimeError(f"the importer created no objects from {path}")
    roots = [obj for obj in created if obj.parent is None or obj.parent not in created]
    if params.get("name") and len(roots) == 1:
        roots[0].name = params["name"]
    meshes = [obj for obj in created if obj.type == "MESH"]
    materials = {slot.material for obj in meshes for slot in obj.material_slots if slot.material is not None}
    return {
        "file_path": path,
        "objects": [{"name": obj.name, "type": obj.type, "parent": obj.parent.name if obj.parent else None}
                    for obj in created],
        "roots": [obj.name for obj in roots],
        "mesh_objects": [obj.name for obj in meshes],
        "tris": sum(_tri_count(obj.data) for obj in meshes),
        "world_bbox": _world_bbox(meshes),
        "materials": sorted(material.name for material in materials),
        "images": _images(materials),
    }


if __name__ == "__main__" and "--params" in sys.argv:
    with open(sys.argv[sys.argv.index("--params") + 1], encoding="utf-8") as _handle:
        print("METAORI_RESULT:" + json.dumps(run(json.load(_handle))))
