"""ComfyClient talks to the ComfyUI HTTP API; httpx.MockTransport stands in for the server."""
import asyncio
import hashlib
import json

import httpx
import pytest

from blender_mcp.metaori.comfy_client import ComfyClient
from blender_mcp.metaori.errors import ComfyRequestError, ComfyUnavailable

PROMPT_ID = "0f0e6f36-2c5e-4e4b-9c55-6f4ef8e0a111"


def _client(handler):
    return ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))


def test_upload_image_posts_multipart_into_the_metaori_folder(tmp_path):
    image = tmp_path / "Ref.PNG"
    image.write_bytes(b"png-bytes")
    expected_name = hashlib.sha256(b"png-bytes").hexdigest()[:16] + ".png"
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"name": expected_name, "subfolder": "metaori", "type": "input"})

    assert asyncio.run(_client(handler).upload_image(image)) == f"metaori/{expected_name}"
    assert seen["path"] == "/upload/image"
    assert expected_name.encode() in seen["body"]
    assert b'name="subfolder"' in seen["body"] and b"metaori" in seen["body"]
    assert b'name="overwrite"' in seen["body"]


def test_queue_prompt_sends_our_prompt_id():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"prompt_id": seen["prompt_id"], "number": 3, "node_errors": {}})

    client = _client(handler)
    asyncio.run(client.queue_prompt({"1": {"class_type": "X", "inputs": {}}}, PROMPT_ID))
    assert seen["prompt_id"] == PROMPT_ID
    assert seen["prompt"] == {"1": {"class_type": "X", "inputs": {}}}
    assert seen["client_id"] == client.client_id


def test_queue_prompt_reports_node_errors():
    body = {
        "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
        "node_errors": {"122": {"class_type": "LoadImage", "errors": [{"message": "Invalid image file"}]}},
    }
    client = _client(lambda request: httpx.Response(400, json=body))
    with pytest.raises(ComfyRequestError, match="failed validation") as info:
        asyncio.run(client.queue_prompt({}, PROMPT_ID))
    assert info.value.details["node_errors"]["122"]["class_type"] == "LoadImage"


def test_history_returns_the_entry_or_none():
    entries = {"a": {"status": {"status_str": "success"}, "outputs": {}}}

    def handler(request):
        prompt_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={prompt_id: entries[prompt_id]} if prompt_id in entries else {})

    client = _client(handler)
    assert asyncio.run(client.history("a"))["status"]["status_str"] == "success"
    assert asyncio.run(client.history("b")) is None


def test_queue_orders_pending_prompts_by_number():
    body = {"queue_running": [[5, "r", {}, {}, []]],
            "queue_pending": [[9, "b", {}, {}, []], [7, "a", {}, {}, []]]}
    state = asyncio.run(_client(lambda request: httpx.Response(200, json=body)).queue())
    assert state.running == ["r"]
    assert state.pending == ["a", "b"]
    assert state.position("b") == 2
    assert state.position("r") is None


def test_download_streams_to_the_destination(tmp_path):
    def handler(request):
        assert request.url.path == "/view"
        assert dict(request.url.params) == {"filename": "m_00001.glb", "subfolder": "metaori/A", "type": "output"}
        return httpx.Response(200, content=b"glTF-data")

    dest = tmp_path / "job" / "model.glb"
    asyncio.run(_client(handler).download("m_00001.glb", "metaori/A", "output", dest))
    assert dest.read_bytes() == b"glTF-data"
    assert not (tmp_path / "job" / "model.glb.part").exists()


def test_failed_download_leaves_no_file(tmp_path):
    dest = tmp_path / "model.glb"
    with pytest.raises(ComfyRequestError, match="HTTP 404"):
        asyncio.run(_client(lambda request: httpx.Response(404)).download("x.glb", "", "output", dest))
    assert list(tmp_path.iterdir()) == []


def test_unreachable_server_raises_comfy_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ComfyUnavailable, match="comfy.test"):
        asyncio.run(_client(handler).system_stats())


def test_cancel_endpoints():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.read())))
        return httpx.Response(200)

    client = _client(handler)
    asyncio.run(client.delete_pending("p1"))
    asyncio.run(client.interrupt("p2"))
    assert seen == [("/queue", {"delete": ["p1"]}), ("/interrupt", {"prompt_id": "p2"})]
