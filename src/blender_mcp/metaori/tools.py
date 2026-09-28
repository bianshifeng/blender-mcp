"""The metaori_* MCP tools: thin wrappers over MetaOriService and the bpy scripts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from .blender_ops import run_script
from .capabilities import validate_asset_name
from .config import MetaOriConfig
from .errors import ParamError
from .service import MetaOriService

MODEL_SUFFIXES = (".glb", ".gltf", ".fbx", ".obj")
SIZE_AXES = ("height", "width", "depth", "longest")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def register_tools(mcp: Any, get_blender_connection: Callable[[], Any],
                   service: MetaOriService | None = None) -> dict[str, Callable]:
    """Register the MetaOri tools on a FastMCP server and return them by name."""
    holder = {"service": service}

    def svc() -> MetaOriService:
        if holder["service"] is None:  # built on first use, so importing the server reads no env
            holder["service"] = MetaOriService(MetaOriConfig.from_env())
        return holder["service"]

    @mcp.tool()
    async def metaori_status() -> str:
        """MetaOri 3D - local AI asset generation on ComfyUI. Call this first.

        Reports whether ComfyUI is reachable (version, GPU memory, queue), each workflow with its
        standard inputs, extra `params` (defaults, allowed values) and missing node classes, the
        prompt purposes, and the folders for job outputs and FBX exports.

        Pipeline: metaori_generate_image (purpose="reference") -> metaori_get_job until succeeded ->
        metaori_generate_model(image_job_id=...) -> metaori_get_job -> metaori_import_model ->
        get_viewport_screenshot -> metaori_normalize_model. Generate one object at a time.
        """
        return _json(await svc().status())

    @mcp.tool()
    async def metaori_generate_image(prompt: str, asset_name: str, purpose: str = "reference",
                                     negative_prompt: str | None = None, width: int | None = None,
                                     height: int | None = None, seed: int | None = None,
                                     count: int | None = None, workflow: str | None = None,
                                     params: dict[str, Any] | None = None, wait_seconds: float = 0) -> str:
        """MetaOri function 1: text-to-image on the local ComfyUI (reference pictures or textures).

        - prompt: what to draw, Chinese or English; describe one object (shape, material, style), or
          the whole space for purpose "concept".
        - asset_name: PascalCase asset name such as BronzeCenser or WoodCrate_A (for concept art the
          scene Mod id, e.g. Mod_GhostVillage). Outputs go to <output_dir>/<asset_name>/<time>_t2i_<job>/
          with job.json (prompt, seed, params, workflow).
        - purpose: "reference" (default) adds a single-object, white-background, 3/4-view template so
          the picture works for metaori_generate_model; "texture" asks for a flat, evenly lit,
          top-down surface (seamless tiling is not guaranteed); "concept" asks for a wide scene view
          from human eye height, 1664x928, no people or text (scene-kickoff concept art); "raw" sends
          the prompt unchanged.
        - negative_prompt: extra things to avoid, added to the purpose's negative prompt.
        - width, height: multiples of 8 in 256-2048 (default from the purpose, usually 1024x1024).
          count: 1-4 images.
        - seed: omit for a random one (recorded). workflow: a workflow id from metaori_status.
          params: that workflow's extras, e.g. {"steps": 30}.
        - wait_seconds: wait up to this long (max 600) for the result; 0 returns at once.

        Returns the job record; outputs.images lists the PNG paths once status is "succeeded".
        A warm run takes about 15 s; the first run after ComfyUI starts loads models (minutes).
        """
        return _json(await svc().generate_image(
            prompt=prompt, asset_name=asset_name, purpose=purpose, negative_prompt=negative_prompt,
            width=width, height=height, seed=seed, count=count, workflow=workflow, params=params,
            wait_seconds=wait_seconds))

    @mcp.tool()
    async def metaori_generate_model(asset_name: str | None = None, image_path: str | None = None,
                                     image_job_id: str | None = None, image_index: int = 0,
                                     seed: int | None = None, target_faces: int | None = None,
                                     texture_size: int | None = None, remove_background: bool | None = None,
                                     workflow: str | None = None, params: dict[str, Any] | None = None,
                                     wait_seconds: float = 0) -> str:
        """MetaOri function 2: image-to-3D on the local ComfyUI; produces one textured GLB.

        - image_job_id: a succeeded metaori_generate_image job (image_index picks the picture and
          asset_name defaults to that job's), OR image_path: a local .png/.jpg/.jpeg/.webp file.
        - asset_name: PascalCase asset name; required with image_path.
        - target_faces: face count after the workflow's decimation (default workflow: 700000). For
          props 30000-100000 bakes cleaner textures than decimating later in Blender.
        - texture_size: 512, 1024, 2048 or 4096 (default 4096; B-tier assets use 2048 at most).
        - remove_background: true by default in the Pixal3D workflow.
        - seed: omit for a random one. workflow / params: see metaori_status,
          e.g. {"model_variant": "trellis2"}.
        - wait_seconds: wait up to this long (max 600); generation takes minutes, so poll with
          metaori_get_job.

        Returns the job record; outputs.model is the GLB path once status is "succeeded".
        """
        return _json(await svc().generate_model(
            asset_name=asset_name, image_path=image_path, image_job_id=image_job_id, image_index=image_index,
            seed=seed, target_faces=target_faces, texture_size=texture_size, remove_background=remove_background,
            workflow=workflow, params=params, wait_seconds=wait_seconds))

    @mcp.tool()
    async def metaori_get_job(job_id: str, wait_seconds: float = 60) -> str:
        """Check a MetaOri job, waiting up to wait_seconds (max 600) for it to finish.

        status is queued (with queue_position), running, succeeded, failed or cancelled. On success
        `outputs` holds local paths (images: list, model: GLB path); a failure carries `error` with
        the ComfyUI node and message. Finished jobs are read from job.json without asking ComfyUI.
        """
        return _json(await svc().get_job(job_id, wait_seconds=wait_seconds))

    @mcp.tool()
    async def metaori_cancel_job(job_id: str) -> str:
        """Cancel a queued or running MetaOri job; returns the job record."""
        return _json(await svc().cancel_job(job_id))

    @mcp.tool()
    async def metaori_import_model(job_id: str | None = None, file_path: str | None = None,
                                   name: str | None = None) -> str:
        """Import into the open Blender scene the GLB of a succeeded metaori_generate_model job
        (job_id) or any .glb/.gltf/.fbx/.obj file (file_path).

        name renames the imported root object when there is exactly one. Returns the created
        objects, the root and mesh object names, their combined world bounding box in metres, the
        triangle count, materials and texture images. Check the facing with get_viewport_screenshot
        before metaori_normalize_model (Pixal3D models arrive with their front facing -Y, so they
        usually need no rotate_deg). Keep the viewport in Solid shading for screenshots
        (shading.color_type = "TEXTURE" shows the albedo): on Blender 5.1 get_viewport_screenshot
        crashes Blender when the viewport is in Material Preview or Rendered (EEVEE) mode.
        """
        if bool(job_id) == bool(file_path):
            raise ParamError("pass exactly one of job_id or file_path")
        if job_id:
            record = await svc().get_job(job_id)
            model = (record.get("outputs") or {}).get("model")
            if record["status"] != "succeeded" or not model:
                raise ParamError(f"job {job_id} is {record['status']} and has no model to import")
            path = Path(model)
        else:
            path = Path(file_path).expanduser()
        if path.suffix.lower() not in MODEL_SUFFIXES:
            raise ParamError(f"file must be one of {list(MODEL_SUFFIXES)}, got {path.name}")
        if not path.is_file():
            raise ParamError(f"model file not found: {path}")
        params = {"file_path": str(path.resolve()), "name": name}
        return _json(await asyncio.to_thread(run_script, get_blender_connection(), "import_model", params))

    @mcp.tool()
    async def metaori_normalize_model(object_name: str, asset_name: str, target_size_m: float | None = None,
                                      size_axis: str = "height", rotate_deg: list[float] | None = None,
                                      target_tris: int | None = None, merge_distance_m: float = 0.0001,
                                      min_island_ratio: float = 0.01, recalc_normals: bool = True,
                                      smooth_angle_deg: float | None = 30.0, apply_modifiers: bool = True,
                                      rename: bool = True, export_fbx: bool = True,
                                      overwrite_export: bool = False) -> str:
        """MetaOri function 3: normalize a Blender model to the asset standard and export FBX for UE.

        Works on a mesh object, or an empty whose child meshes are joined into one. In order:
        apply modifiers, unparent and join; rotate by rotate_deg [x, y, z] degrees (turn the
        model's front to -Y); bake transforms; scale uniformly so size_axis ("height" = Z,
        "width" = X, "depth" = Y or "longest") equals target_size_m metres; merge vertices closer
        than merge_distance_m and delete loose geometry; drop islands with fewer faces than
        min_island_ratio x the largest (0 keeps all); recalculate normals outward; decimate to
        target_tris; clear custom normals and smooth by smooth_angle_deg (None keeps the imported
        normals); put the pivot at the bottom centre on the world origin; rename to
        SM_<asset_name> / MI_<asset_name>; export <export_dir>/<asset_name>/SM_<asset_name>.fbx with
        the UE preset (Blender metres become UE centimetres). An existing FBX is only replaced
        with overwrite_export=true.

        Returns before/after stats (size in metres, tris, non-manifold edges, islands), each
        step's details, the export path and warnings.
        """
        asset_name = validate_asset_name(asset_name)
        if size_axis not in SIZE_AXES:
            raise ParamError(f"size_axis must be one of {list(SIZE_AXES)}, got {size_axis!r}")
        if target_size_m is not None and target_size_m <= 0:
            raise ParamError("target_size_m must be positive")
        if rotate_deg is not None and len(rotate_deg) != 3:
            raise ParamError("rotate_deg needs three angles [x, y, z]")
        if target_tris is not None and target_tris < 4:
            raise ParamError("target_tris must be at least 4")
        if merge_distance_m < 0:
            raise ParamError("merge_distance_m must not be negative")
        if not 0 <= min_island_ratio < 1:
            raise ParamError("min_island_ratio must be in [0, 1)")
        export_path = None
        if export_fbx:
            export_path = svc().config.export_dir / asset_name / f"SM_{asset_name}.fbx"
            if export_path.exists() and not overwrite_export:
                raise ParamError(f"{export_path} already exists; pass overwrite_export=true to replace it")
        params = {
            "object_name": object_name, "asset_name": asset_name, "apply_modifiers": apply_modifiers,
            "rotate_deg": rotate_deg, "target_size_m": target_size_m, "size_axis": size_axis,
            "merge_distance_m": merge_distance_m, "min_island_ratio": min_island_ratio,
            "recalc_normals": recalc_normals, "target_tris": target_tris, "smooth_angle_deg": smooth_angle_deg,
            "rename": rename, "export_path": str(export_path) if export_path else None,
            "overwrite_export": overwrite_export,
        }
        return _json(await asyncio.to_thread(run_script, get_blender_connection(), "normalize", params))

    return {tool.__name__: tool for tool in (
        metaori_status, metaori_generate_image, metaori_generate_model, metaori_get_job,
        metaori_cancel_job, metaori_import_model, metaori_normalize_model)}
