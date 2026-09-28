"""Async client for the parts of the ComfyUI server API that MetaOri uses."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .errors import ComfyRequestError, ComfyUnavailable

UPLOAD_SUBFOLDER = "metaori"
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)


@dataclass(frozen=True)
class QueueState:
    running: list[str]
    pending: list[str]  # in execution order

    def position(self, prompt_id: str) -> int | None:
        """1-based place among pending prompts, or None when not pending."""
        return self.pending.index(prompt_id) + 1 if prompt_id in self.pending else None


class ComfyClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.client_id = uuid.uuid4().hex
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=self._timeout)

    def _unavailable(self, exc: Exception) -> ComfyUnavailable:
        return ComfyUnavailable(
            f"ComfyUI is not reachable at {self.base_url} ({type(exc).__name__}: {exc}). "
            "Start ComfyUI or fix METAORI_COMFYUI_URL.")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise self._unavailable(exc) from exc

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        if response.status_code != 200:
            raise ComfyRequestError(
                f"ComfyUI {method} {path} failed with HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    async def _expect_ok(self, method: str, path: str, **kwargs: Any) -> None:
        response = await self._request(method, path, **kwargs)
        if response.status_code != 200:
            raise ComfyRequestError(f"ComfyUI {method} {path} failed with HTTP {response.status_code}")

    async def system_stats(self) -> dict[str, Any]:
        return await self._json("GET", "/system_stats")

    async def object_info(self) -> dict[str, Any]:
        return await self._json("GET", "/object_info", timeout=120.0)

    async def upload_image(self, path: Path) -> str:
        """Upload into ComfyUI's input/metaori folder; returns the value for a LoadImage node."""
        data = Path(path).read_bytes()
        name = hashlib.sha256(data).hexdigest()[:16] + Path(path).suffix.lower()
        response = await self._request(
            "POST", "/upload/image",
            files={"image": (name, data, "application/octet-stream")},
            data={"type": "input", "subfolder": UPLOAD_SUBFOLDER, "overwrite": "true"},
        )
        if response.status_code != 200:
            raise ComfyRequestError(f"image upload failed with HTTP {response.status_code}: {response.text[:500]}")
        body = response.json()
        subfolder = body.get("subfolder") or ""
        return f"{subfolder}/{body['name']}" if subfolder else body["name"]

    async def queue_prompt(self, graph: dict[str, Any], prompt_id: str) -> None:
        response = await self._request(
            "POST", "/prompt", json={"prompt": graph, "prompt_id": prompt_id, "client_id": self.client_id})
        if response.status_code == 200:
            return
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:500]}}
        error = body.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ComfyRequestError(f"ComfyUI rejected the workflow: {message}",
                                details={"error": error, "node_errors": body.get("node_errors") or {}})

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        body = await self._json("GET", f"/history/{prompt_id}")
        return body.get(prompt_id)

    async def queue(self) -> QueueState:
        body = await self._json("GET", "/queue")

        def ids(items: list) -> list[str]:
            return [item[1] for item in sorted(items, key=lambda item: item[0])]

        return QueueState(running=ids(body.get("queue_running") or []), pending=ids(body.get("queue_pending") or []))

    async def download(self, filename: str, subfolder: str, folder_type: str, dest: Path) -> Path:
        """Stream one output file to dest through a .part file, so a failed download leaves nothing."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        try:
            async with self._client() as client:
                async with client.stream("GET", "/view", params=params, timeout=DOWNLOAD_TIMEOUT) as response:
                    if response.status_code != 200:
                        raise ComfyRequestError(
                            f"download of {subfolder}/{filename} failed with HTTP {response.status_code}")
                    with open(part, "wb") as handle:
                        async for chunk in response.aiter_bytes():
                            handle.write(chunk)
        except httpx.TransportError as exc:
            part.unlink(missing_ok=True)
            raise self._unavailable(exc) from exc
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        os.replace(part, dest)
        return dest

    async def delete_pending(self, prompt_id: str) -> None:
        await self._expect_ok("POST", "/queue", json={"delete": [prompt_id]})

    async def interrupt(self, prompt_id: str) -> None:
        await self._expect_ok("POST", "/interrupt", json={"prompt_id": prompt_id})
