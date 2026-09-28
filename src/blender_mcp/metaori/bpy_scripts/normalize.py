"""normalize - MetaOri bpy script: bring a model to the asset standard and export it for UE.

Runs inside Blender. MetaOri sends this file plus a runner (blender_ops.build_code) through the
addon's execute_code command, and run(params) returns a JSON-serialisable report.
Offline: blender -b scene.blend -P normalize.py -- --params params.json

Order: collect meshes -> apply modifiers -> unparent -> join -> rotate -> bake transforms ->
scale to size -> merge doubles / delete loose -> drop small islands -> recalc normals ->
decimate -> smooth -> pivot to the bottom centre at the origin -> rename -> export FBX.
"""
import json
import math
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

# The UE_VR_Game preset (Tools/blender/export_fbx.py): the scene is in metres and UE reads raw FBX
# values as centimetres, so FBX_SCALE_NONE with global_scale 1.0 writes centimetres.
UE_FBX_SETTINGS = dict(object_types={"MESH"}, global_scale=1.0, apply_unit_scale=True,
                       apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z", axis_up="Y",
                       mesh_smooth_type="FACE", use_mesh_modifiers=True, add_leaf_bones=False,
                       bake_anim=False)
SIZE_AXES = {"width": 0, "depth": 1, "height": 2}
DEFAULTS = dict(apply_modifiers=True, rotate_deg=None, target_size_m=None, size_axis="height",
                merge_distance_m=0.0001, min_island_ratio=0.01, recalc_normals=True, target_tris=None,
                smooth_angle_deg=30.0, rename=True, export_path=None, overwrite_export=False)


def _round(values, digits=5):
    return [round(float(value), digits) for value in values]


def _coords(obj):
    """World-space vertex positions as an (n, 3) array."""
    mesh = obj.data
    flat = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", flat)
    local = flat.reshape(-1, 3).astype(np.float64)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    return local @ matrix[:3, :3].T + matrix[:3, 3]


def _tri_count(mesh):
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return int((totals - 2).sum())


def _face_islands(bm):
    """Groups of faces connected through shared edges."""
    bm.faces.index_update()
    seen = bytearray(len(bm.faces))
    islands = []
    for face in bm.faces:
        if seen[face.index]:
            continue
        seen[face.index] = 1
        stack, island = [face], []
        while stack:
            current = stack.pop()
            island.append(current)
            for edge in current.edges:
                for neighbour in edge.link_faces:
                    if not seen[neighbour.index]:
                        seen[neighbour.index] = 1
                        stack.append(neighbour)
        islands.append(island)
    return islands


def _stats(objects, with_islands=False):
    points = [_coords(obj) for obj in objects if len(obj.data.vertices)]
    if points:
        stacked = np.concatenate(points)
        low, high = stacked.min(axis=0), stacked.max(axis=0)
    else:
        low = high = np.zeros(3)
    stats = {
        "dimensions_m": _round(high - low), "bbox_min": _round(low), "bbox_max": _round(high),
        "verts": 0, "faces": 0, "tris": 0, "non_manifold_edges": 0,
        "uv_layers": sorted({layer.name for obj in objects for layer in obj.data.uv_layers}),
        "materials": sorted({slot.material.name for obj in objects for slot in obj.material_slots if slot.material}),
        "custom_normals": any(getattr(obj.data, "has_custom_normals", False) for obj in objects),
    }
    if with_islands:
        stats["islands"] = 0
    for obj in objects:
        mesh = obj.data
        stats["verts"] += len(mesh.vertices)
        stats["faces"] += len(mesh.polygons)
        stats["tris"] += _tri_count(mesh)
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            stats["non_manifold_edges"] += sum(1 for edge in bm.edges if not edge.is_manifold)
            if with_islands:
                stats["islands"] += len(_face_islands(bm))
        finally:
            bm.free()
    if len(objects) == 1:
        stats["location"] = _round(objects[0].location)
    return stats


def _collect_meshes(obj):
    meshes = [obj] if obj.type == "MESH" else []
    return meshes + [child for child in obj.children_recursive if child.type == "MESH"]


def _bake_modifiers(obj, depsgraph):
    """Replace the mesh with its evaluated version (all modifiers applied)."""
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    old = obj.data
    obj.modifiers.clear()
    obj.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def _clear_custom_normals(obj):
    mesh = obj.data
    if not getattr(mesh, "has_custom_normals", False):
        return
    attribute = mesh.attributes.get("custom_normal")  # Blender 5 stores custom normals as an attribute
    if attribute is not None:
        mesh.attributes.remove(attribute)
    if mesh.has_custom_normals:
        with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                       selected_editable_objects=[obj]):
            bpy.ops.mesh.customdata_custom_splitnormals_clear()


def _smooth(obj, angle_deg):
    mesh = obj.data
    _clear_custom_normals(obj)
    if hasattr(mesh, "set_sharp_from_angle"):  # Blender 4.1+
        mesh.shade_smooth()
        mesh.set_sharp_from_angle(angle=math.radians(angle_deg))
    else:
        for polygon in mesh.polygons:
            polygon.use_smooth = True
        mesh.use_auto_smooth = True
        mesh.auto_smooth_angle = math.radians(angle_deg)


def _used_elsewhere(material, mesh):
    return any(other is not mesh and other.users > 0 and material in other.materials[:] for other in bpy.data.meshes)


def _export(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    view_layer = bpy.context.view_layer
    previous = [other for other in view_layer.objects if other.select_get()]
    active = view_layer.objects.active
    for other in previous:
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    try:
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True, **UE_FBX_SETTINGS)
    finally:
        obj.select_set(False)
        for other in previous:
            other.select_set(True)
        view_layer.objects.active = active
    return {"path": path, "bytes": os.path.getsize(path), "preset": "ue"}


def run(params):
    p = dict(DEFAULTS)
    p.update(params)
    obj = bpy.data.objects.get(p["object_name"])
    if obj is None:
        raise ValueError(f"object {p['object_name']!r} not found")
    asset = p["asset_name"]
    target_name = f"SM_{asset}"
    steps, warnings = [], []
    scale_length = bpy.context.scene.unit_settings.scale_length
    if abs(scale_length - 1.0) > 1e-6:
        warnings.append(f"scene unit scale is {scale_length}, not 1.0; sizes are in Blender units")

    meshes = _collect_meshes(obj)
    if not meshes:
        raise ValueError(f"{obj.name} has no mesh objects")
    for mesh_obj in meshes:
        if mesh_obj.find_armature() is not None or any(m.type == "ARMATURE" for m in mesh_obj.modifiers):
            raise ValueError(f"{mesh_obj.name} is skinned; normalization is for static meshes")
    if p["target_tris"] and not p["apply_modifiers"] and any(mesh_obj.modifiers for mesh_obj in meshes):
        raise ValueError("target_tris needs apply_modifiers=true when the mesh has modifiers")
    if p["rename"]:
        clash = bpy.data.objects.get(target_name)
        if clash is not None and clash not in meshes:
            raise ValueError(f"an object named {target_name} already exists; rename or delete it first")
    export_path = p["export_path"]
    if export_path and os.path.exists(export_path) and not p["overwrite_export"]:
        raise FileExistsError(f"{export_path} exists; pass overwrite_export=true to replace it")

    before = _stats(meshes)

    for mesh_obj in meshes:
        if mesh_obj.data.users > 1:
            mesh_obj.data = mesh_obj.data.copy()
    if p["apply_modifiers"]:
        with_modifiers = [mesh_obj for mesh_obj in meshes if mesh_obj.modifiers]
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for mesh_obj in with_modifiers:
            _bake_modifiers(mesh_obj, depsgraph)
        if with_modifiers:
            steps.append({"step": "apply_modifiers", "objects": len(with_modifiers)})

    for mesh_obj in meshes:
        if mesh_obj.parent is not None:
            world = mesh_obj.matrix_world.copy()
            mesh_obj.parent = None
            mesh_obj.matrix_world = world

    target = obj if obj.type == "MESH" else meshes[0]
    if len(meshes) > 1:
        with bpy.context.temp_override(active_object=target, object=target, selected_objects=meshes,
                                       selected_editable_objects=meshes):
            bpy.ops.object.join()
        steps.append({"step": "join", "objects": len(meshes)})
    if obj is not target:
        warnings.append(f"root {obj.type.lower()} {obj.name!r} was left in place; its meshes were joined")

    mesh = target.data
    matrix = target.matrix_world.copy()
    if p["rotate_deg"]:
        if len(p["rotate_deg"]) != 3:
            raise ValueError("rotate_deg needs three angles [x, y, z]")
        rotation = Euler([math.radians(angle) for angle in p["rotate_deg"]], "XYZ").to_matrix().to_4x4()
        matrix = rotation @ matrix
        steps.append({"step": "rotate", "deg": _round(p["rotate_deg"], 3)})
    mesh.transform(matrix)
    if matrix.determinant() < 0 and hasattr(mesh, "flip_normals"):
        mesh.flip_normals()
    target.matrix_world = Matrix.Identity(4)
    steps.append({"step": "bake_transform"})

    if p["target_size_m"] is not None:
        size = float(p["target_size_m"])
        axis = p["size_axis"]
        if size <= 0:
            raise ValueError("target_size_m must be positive")
        if axis != "longest" and axis not in SIZE_AXES:
            raise ValueError(f"size_axis must be height, width, depth or longest, got {axis!r}")
        coords = _coords(target)
        dims = coords.max(axis=0) - coords.min(axis=0)
        current = float(dims.max() if axis == "longest" else dims[SIZE_AXES[axis]])
        if current <= 0:
            raise ValueError(f"the model has no extent along {axis}")
        factor = size / current
        mesh.transform(Matrix.Scale(factor, 4))
        steps.append({"step": "scale", "axis": axis, "target_m": size, "factor": round(factor, 6)})

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        verts_before = len(bm.verts)
        if p["merge_distance_m"] and p["merge_distance_m"] > 0:
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=float(p["merge_distance_m"]))
        merged = verts_before - len(bm.verts)
        loose_edges = [edge for edge in bm.edges if not edge.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
        loose_verts = [vert for vert in bm.verts if not vert.link_edges]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
        steps.append({"step": "merge_and_loose", "merged_verts": merged, "loose_edges": len(loose_edges),
                      "loose_verts": len(loose_verts)})
        ratio = float(p["min_island_ratio"] or 0)
        if ratio > 0 and len(bm.faces):
            islands = _face_islands(bm)
            largest = max(len(island) for island in islands)
            small = [island for island in islands if len(island) < ratio * largest]
            doomed = [face for island in small for face in island]
            if doomed:
                bmesh.ops.delete(bm, geom=doomed, context="FACES")
            steps.append({"step": "islands", "found": len(islands), "removed": len(small),
                          "removed_faces": len(doomed)})
        if p["recalc_normals"]:
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            steps.append({"step": "recalc_normals"})
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()

    if p["target_tris"]:
        budget = int(p["target_tris"])
        tris = _tri_count(mesh)
        if tris > budget:
            modifier = target.modifiers.new("MetaOriDecimate", "DECIMATE")
            modifier.decimate_type = "COLLAPSE"
            modifier.ratio = budget / tris
            modifier.use_collapse_triangulate = True
            _bake_modifiers(target, bpy.context.evaluated_depsgraph_get())
            mesh = target.data
            steps.append({"step": "decimate", "ratio": round(budget / tris, 5), "tris_before": tris,
                          "tris_after": _tri_count(mesh)})
        else:
            steps.append({"step": "decimate", "skipped": f"{tris} tris already within {budget}"})

    if p["smooth_angle_deg"] is not None:
        _smooth(target, float(p["smooth_angle_deg"]))
        steps.append({"step": "smooth", "angle_deg": float(p["smooth_angle_deg"])})

    coords = _coords(target)
    if not len(coords):
        raise ValueError("no geometry is left after cleanup")
    low, high = coords.min(axis=0), coords.max(axis=0)
    pivot = Vector(((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]))
    mesh.transform(Matrix.Translation(-pivot))
    mesh.update()
    target.matrix_world = Matrix.Identity(4)
    steps.append({"step": "pivot", "moved_by": _round(-pivot)})

    if p["rename"]:
        target.name = target_name
        mesh.name = target_name
        materials = []
        for slot in target.material_slots:
            if slot.material is not None and slot.material not in materials:
                materials.append(slot.material)
        renamed = []
        for index, material in enumerate(materials, start=1):
            if _used_elsewhere(material, mesh):
                copy = material.copy()
                for slot in target.material_slots:
                    if slot.material == material:
                        slot.material = copy
                material = copy
            material.name = f"MI_{asset}" if len(materials) == 1 else f"MI_{asset}_{index:02d}"
            renamed.append(material.name)
        steps.append({"step": "rename", "object": target.name, "materials": renamed})
        if target.name != target_name:
            warnings.append(f"the object is named {target.name!r} because {target_name} is taken")

    export = None
    if export_path:
        export = _export(target, export_path)
        steps.append({"step": "export", **export})

    return {"object": target.name, "mesh": target.data.name, "asset_name": asset, "before": before,
            "after": _stats([target], with_islands=True), "steps": steps, "export": export,
            "warnings": warnings}


if __name__ == "__main__" and "--params" in sys.argv:
    with open(sys.argv[sys.argv.index("--params") + 1], encoding="utf-8") as _handle:
        print("METAORI_RESULT:" + json.dumps(run(json.load(_handle))))
