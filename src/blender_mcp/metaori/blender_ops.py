"""Run MetaOri's bpy scripts inside Blender through the addon's execute_code command.

Each script in bpy_scripts/ defines run(params) -> dict. build_code() appends a runner that
calls it with the JSON parameters and prints one marker line holding the JSON result or the
error. The runner never lets an exception escape execute_code, because an addon-side error
makes the MCP server drop its socket to Blender.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BlenderScriptError

SCRIPTS_DIR = Path(__file__).with_name("bpy_scripts")
RESULT_MARK = "METAORI_RESULT:"
ERROR_MARK = "METAORI_ERROR:"

_RUNNER = """


def _metaori_main():
    import json as _json
    import traceback as _traceback
    try:
        _line = {result_mark} + _json.dumps(run(_json.loads({params})))
    except Exception as _exc:
        _line = {error_mark} + _json.dumps({{"type": type(_exc).__name__, "message": str(_exc),
                                             "traceback": _traceback.format_exc()}})
    print(_line)


_metaori_main()
"""


def build_code(script: str, params: dict[str, Any], scripts_dir: Path = SCRIPTS_DIR) -> str:
    source = (Path(scripts_dir) / f"{script}.py").read_text(encoding="utf-8")
    return source + _RUNNER.format(params=repr(json.dumps(params)), result_mark=repr(RESULT_MARK),
                                   error_mark=repr(ERROR_MARK))


def parse_output(script: str, output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
        if line.startswith(ERROR_MARK):
            error = json.loads(line[len(ERROR_MARK):])
            raise BlenderScriptError(
                f"{script} failed in Blender: {error.get('type')}: {error.get('message')}\n"
                f"{error.get('traceback', '')}".rstrip())
    raise BlenderScriptError(f"{script} printed no result; Blender output:\n{output[-2000:]}")


def run_script(blender: Any, script: str, params: dict[str, Any], scripts_dir: Path = SCRIPTS_DIR) -> dict[str, Any]:
    """Run bpy_scripts/<script>.py in the connected Blender and return its result dict."""
    code = build_code(script, params, scripts_dir)
    try:
        response = blender.send_command("execute_code", {"code": code})
    except Exception as exc:
        raise BlenderScriptError(f"Blender could not run {script}: {exc}") from exc
    output = response.get("result", "") if isinstance(response, dict) else str(response)
    return parse_output(script, output)
