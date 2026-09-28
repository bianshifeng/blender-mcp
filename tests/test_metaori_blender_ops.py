"""bpy scripts travel to Blender as code plus a runner; results come back as one marker line."""
import contextlib
import io

import pytest

from blender_mcp.metaori.blender_ops import build_code, parse_output, run_script
from blender_mcp.metaori.errors import BlenderScriptError

PARAMS = {"file_path": "C:\\Assets\\\u6a21\u578b\\model.glb", "name": None, "size": [1, 2.5]}


def _scripts(tmp_path, body):
    (tmp_path / "echo.py").write_text(body, encoding="utf-8")
    return tmp_path


def _exec(code):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(code, {"bpy": None})  # the namespace execute_code gives scripts
    return buffer.getvalue()


def test_params_round_trip_through_the_runner(tmp_path):
    code = build_code("echo", PARAMS, _scripts(tmp_path, "def run(params):\n    return params\n"))
    assert parse_output("echo", _exec(code)) == PARAMS


def test_script_errors_come_back_as_blender_script_errors(tmp_path):
    code = build_code("echo", {}, _scripts(tmp_path, "def run(params):\n    raise ValueError('boom')\n"))
    output = _exec(code)
    assert output.startswith("METAORI_ERROR:")
    with pytest.raises(BlenderScriptError, match="ValueError: boom"):
        parse_output("echo", output)


def test_unserialisable_results_are_reported_not_raised(tmp_path):
    code = build_code("echo", {}, _scripts(tmp_path, "def run(params):\n    return {'x': object()}\n"))
    with pytest.raises(BlenderScriptError, match="TypeError"):
        parse_output("echo", _exec(code))


def test_output_without_a_marker_is_an_error():
    with pytest.raises(BlenderScriptError, match="printed no result"):
        parse_output("echo", "some log line\n")


class FakeBlender:
    def __init__(self, result=None, error=None):
        self.sent = []
        self.result = result
        self.error = error

    def send_command(self, command, params=None):
        self.sent.append((command, params))
        if self.error:
            raise self.error
        return {"executed": True, "result": self.result}


def test_run_script_sends_execute_code_and_parses_the_last_marker(tmp_path):
    blender = FakeBlender(result='log\nMETAORI_RESULT:{"ok": 1}\n')
    scripts = _scripts(tmp_path, "def run(params):\n    return params\n")
    assert run_script(blender, "echo", {"a": 1}, scripts) == {"ok": 1}
    command, params = blender.sent[0]
    assert command == "execute_code" and "def run(params)" in params["code"]


def test_connection_failures_are_wrapped(tmp_path):
    blender = FakeBlender(error=Exception("Could not connect to Blender"))
    with pytest.raises(BlenderScriptError, match="Blender could not run echo"):
        run_script(blender, "echo", {}, _scripts(tmp_path, "def run(params):\n    return {}\n"))
