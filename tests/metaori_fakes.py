"""Test doubles shared by the MetaOri service and tool tests."""
from __future__ import annotations

from pathlib import Path

from blender_mcp.metaori.comfy_client import QueueState
from blender_mcp.metaori.config import MetaOriConfig
from blender_mcp.metaori.errors import ComfyUnavailable
from blender_mcp.metaori.service import MetaOriService


class FakeComfy:
    """In-memory stand-in for ComfyClient, with helpers that move jobs through their states."""

    def __init__(self):
        self.reachable = True
        self.object_info_classes: set[str] = set()
        self.uploads: list[Path] = []
        self.submitted: list[tuple[str, dict]] = []
        self.running: list[str] = []
        self.pending: list[str] = []
        self.histories: dict[str, dict] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.downloads: list[Path] = []
        self.deleted: list[str] = []
        self.interrupted: list[str] = []
        self.submit_error: Exception | None = None

    def _check(self):
        if not self.reachable:
            raise ComfyUnavailable("ComfyUI is not reachable at http://comfy.test")

    async def system_stats(self):
        self._check()
        return {"system": {"comfyui_version": "0.37.0"},
                "devices": [{"name": "cuda:0 RTX 4090", "vram_total": 24 * 2**30, "vram_free": 8 * 2**30}]}

    async def object_info(self):
        self._check()
        return {name: {} for name in self.object_info_classes}

    async def upload_image(self, path):
        self._check()
        self.uploads.append(Path(path))
        return f"metaori/{Path(path).name}"

    async def queue_prompt(self, graph, prompt_id):
        self._check()
        if self.submit_error:
            raise self.submit_error
        self.submitted.append((prompt_id, graph))
        self.pending.append(prompt_id)

    async def history(self, prompt_id):
        self._check()
        return self.histories.get(prompt_id)

    async def queue(self):
        self._check()
        return QueueState(running=list(self.running), pending=list(self.pending))

    async def download(self, filename, subfolder, folder_type, dest):
        self._check()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[(subfolder, filename)])
        self.downloads.append(dest)
        return dest

    async def delete_pending(self, prompt_id):
        self.deleted.append(prompt_id)
        self.pending.remove(prompt_id)

    async def interrupt(self, prompt_id):
        self.interrupted.append(prompt_id)

    def start(self, prompt_id):
        self.pending.remove(prompt_id)
        self.running.append(prompt_id)

    def _leave_queue(self, prompt_id):
        for queue in (self.pending, self.running):
            if prompt_id in queue:
                queue.remove(prompt_id)

    def succeed(self, prompt_id, node, key, files):
        """files: list of (subfolder, filename, bytes) the output node reports."""
        self._leave_queue(prompt_id)
        items = []
        for subfolder, filename, data in files:
            self.files[(subfolder, filename)] = data
            items.append({"filename": filename, "subfolder": subfolder, "type": "output"})
        self.histories[prompt_id] = {
            "status": {"status_str": "success", "completed": True, "messages": [
                ["execution_start", {"timestamp": 1_000}], ["execution_success", {"timestamp": 13_500}]]},
            "outputs": {node: {key: items}},
        }

    def fail(self, prompt_id, message="CUDA out of memory"):
        self._leave_queue(prompt_id)
        self.histories[prompt_id] = {
            "status": {"status_str": "error", "completed": False, "messages": [
                ["execution_start", {"timestamp": 1_000}],
                ["execution_error", {"node_id": "458", "node_type": "KSampler", "exception_message": message,
                                     "exception_type": "torch.OutOfMemoryError", "timestamp": 2_000}]]},
            "outputs": {},
        }

    def interrupt_history(self, prompt_id):
        self._leave_queue(prompt_id)
        self.histories[prompt_id] = {
            "status": {"status_str": "error", "completed": False, "messages": [
                ["execution_start", {"timestamp": 1_000}], ["execution_interrupted", {"timestamp": 1_500}]]},
            "outputs": {},
        }


class FakeClock:
    """Monotonic clock plus an async sleep that advances it; on_sleep lets a test act mid-wait."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


def make_service(tmp_path, comfy=None, clock=None):
    comfy = comfy or FakeComfy()
    clock = clock or FakeClock()
    config = MetaOriConfig(comfyui_url="http://comfy.test", output_dir=tmp_path / "out",
                           export_dir=tmp_path / "export")
    return MetaOriService(config, client=comfy, sleep=clock.sleep, clock=clock), comfy, clock
