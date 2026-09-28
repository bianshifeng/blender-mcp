"""MetaOri orchestration: validate, bind, submit, poll and download.

The MCP tools in tools.py are thin wrappers around MetaOriService. It only needs a ComfyUI
client, the workflow registry and a job store, so it is tested against fakes.
"""
from __future__ import annotations

import asyncio
import random
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .capabilities import IMAGE_TO_3D, MAX_SEED, TEXT_TO_IMAGE, compose_prompt, load_presets, validate_asset_name
from .comfy_client import ComfyClient
from .config import MetaOriConfig
from .errors import ComfyRequestError, ComfyUnavailable, ParamError, WorkflowError
from .jobs import TERMINAL, JobStore, new_job_id, now_iso
from .workflows import WorkflowPack, WorkflowRegistry

POLL_INTERVAL = 2.0
MAX_WAIT_SECONDS = 600.0
OBJECT_INFO_TTL = 300.0
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
FINISH_EVENTS = ("execution_success", "execution_error", "execution_interrupted")


def random_seed() -> int:
    return random.randint(0, MAX_SEED)


def _output_name(name: str, index: int, total: int, source_filename: str) -> str:
    suffix = Path(source_filename).suffix.lower() or ".bin"
    if name == "images":
        return f"image_{index:02d}{suffix}"
    return f"{name}{suffix}" if total == 1 else f"{name}_{index:02d}{suffix}"


def _execution_seconds(events: dict[str, dict]) -> float | None:
    start = (events.get("execution_start") or {}).get("timestamp")
    end = next((events[key].get("timestamp") for key in FINISH_EVENTS if key in events), None)
    if start is None or end is None:
        return None
    return round((end - start) / 1000.0, 1)


def _seconds_between(start: str | None, end: str) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


class MetaOriService:
    """Everything the metaori_* tools do, minus MCP and Blender."""

    def __init__(self, config: MetaOriConfig, *, client: Any = None, registry: WorkflowRegistry | None = None,
                 store: JobStore | None = None, presets: dict | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.client = client if client is not None else ComfyClient(config.comfyui_url)
        self.registry = registry if registry is not None else WorkflowRegistry.load()
        self.store = store if store is not None else JobStore(config.output_dir)
        self.presets = presets if presets is not None else load_presets()
        self._sleep = sleep
        self._clock = clock
        self._object_info: dict[str, Any] | None = None
        self._object_info_at = 0.0

    # ------------------------------------------------------------------ status

    async def status(self) -> dict[str, Any]:
        comfy: dict[str, Any] = {"url": self.config.comfyui_url, "reachable": False}
        object_info = None
        try:
            stats = await self.client.system_stats()
            queue = await self.client.queue()
            object_info = await self._cached_object_info()
        except (ComfyUnavailable, ComfyRequestError) as exc:
            comfy["error"] = str(exc)
        else:
            comfy.update(
                reachable=True,
                version=(stats.get("system") or {}).get("comfyui_version"),
                devices=[{"name": device.get("name"),
                          "vram_total_gb": round((device.get("vram_total") or 0) / 2**30, 1),
                          "vram_free_gb": round((device.get("vram_free") or 0) / 2**30, 1)}
                         for device in stats.get("devices") or []],
                queue_running=len(queue.running),
                queue_pending=len(queue.pending),
            )
        workflows = []
        for pack in sorted(self.registry.packs.values(), key=lambda p: (p.capability.name, p.id)):
            info = pack.describe()
            if object_info is not None:
                missing = sorted(pack.required_classes() - set(object_info))
                info.update(available=not missing, missing_node_classes=missing)
            workflows.append(info)
        return {
            "comfyui": comfy,
            "workflows": workflows,
            "rejected_workflows": dict(self.registry.errors),
            "purposes": {name: preset.description for name, preset in self.presets.items()},
            "output_dir": str(self.config.output_dir),
            "export_dir": str(self.config.export_dir),
        }

    async def _cached_object_info(self) -> dict[str, Any]:
        if self._object_info is None or self._clock() - self._object_info_at > OBJECT_INFO_TTL:
            self._object_info = await self.client.object_info()
            self._object_info_at = self._clock()
        return self._object_info

    # -------------------------------------------------------------- generation

    async def generate_image(self, *, prompt: str, asset_name: str, purpose: str = "reference",
                             negative_prompt: str | None = None, width: int | None = None,
                             height: int | None = None, seed: int | None = None, count: int | None = None,
                             workflow: str | None = None, params: dict[str, Any] | None = None,
                             wait_seconds: float = 0) -> dict[str, Any]:
        asset_name = validate_asset_name(asset_name)
        preset = self.presets.get(purpose)
        if preset is None:
            raise ParamError(f"purpose must be one of {sorted(self.presets)}, got {purpose!r}")
        pack = self.registry.get(TEXT_TO_IMAGE.name, workflow)
        composed = compose_prompt(preset, prompt, negative_prompt)
        job_id = new_job_id()
        explicit = {"prompt": composed.positive, "width": width, "height": height, "seed": seed, "count": count}
        if negative_prompt and negative_prompt.strip():
            explicit["negative_prompt"] = composed.negative
        soft = {"seed": random_seed(), "width": preset.width, "height": preset.height,
                "filename_prefix": self._prefix(asset_name, pack, job_id)}
        if composed.negative and "negative_prompt" not in explicit:
            soft["negative_prompt"] = composed.negative
        applied, skipped = self._plan_params(pack, explicit, soft)
        record = {
            "capability": pack.capability.name,
            "purpose": purpose,
            "prompt": {"user": prompt, "positive": composed.positive, "negative": applied.get("negative_prompt")},
        }
        return await self._submit(pack, job_id, asset_name, applied, skipped, params, record, wait_seconds)

    async def generate_model(self, *, asset_name: str | None = None, image_path: str | None = None,
                             image_job_id: str | None = None, image_index: int = 0, seed: int | None = None,
                             target_faces: int | None = None, texture_size: int | None = None,
                             remove_background: bool | None = None, workflow: str | None = None,
                             params: dict[str, Any] | None = None, wait_seconds: float = 0) -> dict[str, Any]:
        if bool(image_path) == bool(image_job_id):
            raise ParamError("pass exactly one of image_path or image_job_id")
        source: dict[str, Any] = {}
        if image_job_id:
            image_job = self.store.load(image_job_id)
            images = (image_job.get("outputs") or {}).get("images") or []
            if image_job.get("status") != "succeeded" or not images:
                raise ParamError(f"image job {image_job_id} is {image_job.get('status')}; wait until it has succeeded")
            if isinstance(image_index, bool) or not isinstance(image_index, int) or not 0 <= image_index < len(images):
                raise ParamError(f"image_index must be between 0 and {len(images) - 1}")
            path = Path(images[image_index])
            asset_name = asset_name or image_job.get("asset_name")
            source.update(image_job_id=image_job["job_id"], image_index=image_index)
        else:
            path = Path(image_path).expanduser()
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ParamError(f"image must be one of {list(IMAGE_SUFFIXES)}, got {path.name}")
        if not path.is_file():
            raise ParamError(f"image not found: {path}")
        asset_name = validate_asset_name(asset_name)
        pack = self.registry.get(IMAGE_TO_3D.name, workflow)
        job_id = new_job_id()
        explicit = {"seed": seed, "target_faces": target_faces, "texture_size": texture_size,
                    "remove_background": remove_background}
        soft = {"seed": random_seed(), "filename_prefix": self._prefix(asset_name, pack, job_id)}
        applied, skipped = self._plan_params(pack, explicit, soft)
        pack.convert_extras(params)  # fail on bad extras before uploading anything
        uploaded = await self.client.upload_image(path)
        applied["image"] = IMAGE_TO_3D.inputs["image"].validate(uploaded)
        copy_name = f"input{path.suffix.lower()}"
        source.update(image_path=str(path.resolve()), uploaded_as=uploaded, copied_as=copy_name)
        record = {"capability": pack.capability.name, "source": source}
        return await self._submit(pack, job_id, asset_name, applied, skipped, params, record, wait_seconds,
                                  copy_inputs={copy_name: path})

    @staticmethod
    def _prefix(asset_name: str, pack: WorkflowPack, job_id: str) -> str:
        return f"metaori/{asset_name}/{pack.capability.kind}_{job_id[:8]}"

    @staticmethod
    def _plan_params(pack: WorkflowPack, explicit: dict[str, Any],
                     soft: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Explicit params must be supported by the pack; unsupported soft defaults are skipped."""
        explicit = pack.capability.validate(explicit)
        soft = pack.capability.validate(soft)
        unsupported = sorted(key for key in explicit if not pack.supports(key))
        if unsupported:
            raise WorkflowError(
                f"workflow {pack.id} does not support {unsupported}; its inputs are {sorted(pack.inputs)}")
        applied = dict(explicit)
        skipped = []
        for key, value in soft.items():
            if key in applied:
                continue
            if pack.supports(key):
                applied[key] = value
            else:
                skipped.append(key)
        return applied, sorted(skipped)

    async def _submit(self, pack: WorkflowPack, job_id: str, asset_name: str, applied: dict[str, Any],
                      skipped: list[str], extras: dict[str, Any] | None, record: dict[str, Any],
                      wait_seconds: float, copy_inputs: dict[str, Path] | None = None) -> dict[str, Any]:
        graph = pack.bind(applied, extras)
        folder, record = self.store.create(job_id=job_id, kind=pack.capability.kind, asset_name=asset_name, record={
            **record,
            "workflow": {"id": pack.id, "sha256": pack.graph_sha256},
            "params": applied,
            "skipped_params": skipped,
            "extras": pack.resolve_extras(extras),
            "comfyui": {"url": self.config.comfyui_url, "prompt_id": job_id},
            "status": "submitting",
            "queue_position": None,
            "error": None,
            "outputs": {},
        })
        for name, source in (copy_inputs or {}).items():
            shutil.copy2(source, folder / name)
        try:
            await self.client.queue_prompt(graph, job_id)
        except ComfyRequestError as exc:
            return self._finish(record, "failed", error={"message": str(exc), **exc.details})
        except ComfyUnavailable as exc:
            return self._finish(record, "failed", error={"message": str(exc)})
        record["status"] = "queued"
        self.store.save(record)
        return await self.get_job(job_id, wait_seconds=wait_seconds)

    # -------------------------------------------------------------------- jobs

    async def get_job(self, job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        record = self.store.load(job_id)
        wait = min(max(float(wait_seconds or 0), 0.0), MAX_WAIT_SECONDS)
        deadline = self._clock() + wait
        while record["status"] not in TERMINAL:
            record = await self._refresh(record)
            remaining = deadline - self._clock()
            if record["status"] in TERMINAL or remaining <= 0:
                break
            await self._sleep(min(POLL_INTERVAL, remaining))
        return record

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        record = self.store.load(job_id)
        if record["status"] in TERMINAL:
            return record
        queue = await self.client.queue()
        if job_id in queue.pending:
            await self.client.delete_pending(job_id)
            return self._finish(record, "cancelled")
        if job_id in queue.running:
            await self.client.interrupt(job_id)
            return await self.get_job(job_id, wait_seconds=15)
        return await self.get_job(job_id)

    async def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = record["job_id"]
        entry = await self.client.history(job_id)
        if entry is not None:
            return await self._complete(record, entry)
        queue = await self.client.queue()
        if job_id in queue.running:
            status, position = "running", None
        elif job_id in queue.pending:
            status, position = "queued", queue.position(job_id)
        else:
            return self._finish(record, "failed", error={
                "reason": "lost",
                "message": "ComfyUI no longer knows this job; it was restarted or its history was cleared"})
        if status == "running" and not record.get("started_at"):
            record["started_at"] = now_iso()
        if record["status"] != status or record.get("queue_position") != position:
            record.update(status=status, queue_position=position)
            self.store.save(record)
        return record

    async def _complete(self, record: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        status = entry.get("status") or {}
        events = {message[0]: message[1] for message in status.get("messages") or []
                  if isinstance(message, (list, tuple)) and len(message) == 2 and isinstance(message[1], dict)}
        timing = _execution_seconds(events)
        if "execution_interrupted" in events:
            return self._finish(record, "cancelled", execution_s=timing)
        if status.get("status_str") != "success":
            error = events.get("execution_error") or {}
            return self._finish(record, "failed", execution_s=timing, error={
                "message": error.get("exception_message") or "ComfyUI reported an error without details",
                "node_id": error.get("node_id"),
                "node_type": error.get("node_type"),
                "exception_type": error.get("exception_type"),
            })
        pack = self.registry.packs.get(record["workflow"]["id"])
        if pack is None:
            return self._finish(record, "failed", execution_s=timing, error={
                "message": f"workflow {record['workflow']['id']} is no longer installed; its outputs cannot be mapped"})
        folder = Path(record["folder"])
        node_outputs = entry.get("outputs") or {}
        outputs: dict[str, Any] = {}
        for name, spec in pack.outputs.items():
            files = (node_outputs.get(spec.node) or {}).get(spec.key) or []
            if not files:
                return self._finish(record, "failed", execution_s=timing, outputs=outputs, error={
                    "message": f"ComfyUI finished but node {spec.node} ({spec.class_type}) returned no {spec.key!r} output"})
            paths = []
            for index, item in enumerate(files, start=1):
                dest = folder / _output_name(name, index, len(files), item["filename"])
                try:
                    await self.client.download(item["filename"], item.get("subfolder", ""), item.get("type", "output"), dest)
                except (ComfyRequestError, OSError) as exc:
                    return self._finish(record, "failed", execution_s=timing, outputs=outputs,
                                        error={"message": f"downloading {item['filename']} failed: {exc}"})
                paths.append(str(dest))
            outputs[name] = paths if name == "images" else paths[0]
        return self._finish(record, "succeeded", execution_s=timing, outputs=outputs)

    def _finish(self, record: dict[str, Any], status: str, *, error: dict[str, Any] | None = None,
                outputs: dict[str, Any] | None = None, execution_s: float | None = None) -> dict[str, Any]:
        record["status"] = status
        record["queue_position"] = None
        if error is not None:
            record["error"] = error
        if outputs is not None:
            record["outputs"] = outputs
        if execution_s is not None:
            record["execution_s"] = execution_s
        record["finished_at"] = now_iso()
        record["duration_s"] = _seconds_between(record.get("created_at"), record["finished_at"])
        self.store.save(record)
        return record
