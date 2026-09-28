"""Workflow packs: an untouched ComfyUI API graph plus a manifest binding standard parameters to nodes.

To swap in a better workflow, export it from ComfyUI with "Export (API)", save it as
workflows/<id>/workflow_api.json and describe it in workflows/<id>/manifest.json
(see docs/metaori.md). No code changes are needed.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capabilities import CAPABILITIES, Capability
from .errors import ParamError, WorkflowError

MANIFEST_SCHEMA = "metaori.workflow/1"
WORKFLOWS_DIR = Path(__file__).with_name("workflows")
EXTRA_TYPES = ("bool", "enum", "float", "int", "str")


@dataclass(frozen=True)
class Target:
    node: str
    class_type: str
    input: str


@dataclass(frozen=True)
class Extra:
    name: str
    type: str
    description: str
    default: Any
    targets: tuple[Target, ...]
    min: float | None = None
    max: float | None = None
    values: dict[str, Any] | None = None

    def convert(self, value: Any) -> Any:
        """Validate a caller value and return the raw value written into the graph."""
        if self.type == "enum":
            if not isinstance(value, str) or value not in self.values:
                raise ParamError(f"{self.name} must be one of {sorted(self.values)}, got {value!r}")
            return self.values[value]
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ParamError(f"{self.name} must be true or false, got {value!r}")
            return value
        if self.type == "str":
            if not isinstance(value, str):
                raise ParamError(f"{self.name} must be a string, got {value!r}")
            return value
        if self.type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ParamError(f"{self.name} must be an integer, got {value!r}")
        if self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParamError(f"{self.name} must be a number, got {value!r}")
            value = float(value)
        if self.min is not None and value < self.min:
            raise ParamError(f"{self.name} must be >= {self.min}, got {value}")
        if self.max is not None and value > self.max:
            raise ParamError(f"{self.name} must be <= {self.max}, got {value}")
        return value

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"type": self.type, "description": self.description, "default": self.default}
        if self.values is not None:
            info["values"] = sorted(self.values)
        if self.min is not None:
            info["min"] = self.min
        if self.max is not None:
            info["max"] = self.max
        return info


@dataclass(frozen=True)
class Output:
    name: str
    node: str
    class_type: str
    key: str


@dataclass
class WorkflowPack:
    id: str
    capability: Capability
    description: str
    default: bool
    graph: dict[str, Any]
    graph_sha256: str
    inputs: dict[str, tuple[Target, ...]]
    extras: dict[str, Extra]
    outputs: dict[str, Output]
    directory: Path

    def supports(self, param: str) -> bool:
        return param in self.inputs

    def convert_extras(self, extras: dict[str, Any] | None) -> dict[str, Any]:
        """Validate caller extras; returns name -> raw graph value."""
        extras = extras or {}
        unknown = sorted(set(extras) - set(self.extras))
        if unknown:
            raise ParamError(
                f"workflow {self.id} has no extra parameters {unknown}; available: {sorted(self.extras)}")
        return {name: self.extras[name].convert(value) for name, value in extras.items()}

    def bind(self, params: dict[str, Any], extras: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a copy of the graph with standard params and extras written into their targets.

        A target that currently holds a link ([node_id, output_index]) is replaced by the constant.
        """
        unsupported = sorted(set(params) - set(self.inputs))
        if unsupported:
            raise WorkflowError(
                f"workflow {self.id} does not support {unsupported}; its inputs are {sorted(self.inputs)}")
        raw_extras = self.convert_extras(extras)
        graph = copy.deepcopy(self.graph)
        for name, value in params.items():
            for target in self.inputs[name]:
                graph[target.node]["inputs"][target.input] = value
        for name, value in raw_extras.items():
            for target in self.extras[name].targets:
                graph[target.node]["inputs"][target.input] = value
        return graph

    def resolve_extras(self, extras: dict[str, Any] | None) -> dict[str, Any]:
        """Caller extras merged over the manifest defaults, as names (for the job record)."""
        merged = {name: extra.default for name, extra in self.extras.items()}
        merged.update(extras or {})
        return merged

    def required_classes(self) -> set[str]:
        return {node["class_type"] for node in self.graph.values()}

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability.name,
            "default": self.default,
            "description": self.description,
            "inputs": sorted(k for k in self.inputs if k not in self.capability.internal_inputs),
            "extras": {name: extra.describe() for name, extra in sorted(self.extras.items())},
            "outputs": sorted(self.outputs),
        }


def _targets(raw: Any, graph: dict[str, Any], where: str) -> tuple[Target, ...]:
    if not isinstance(raw, list) or not raw:
        raise WorkflowError(f"{where}: bind a non-empty list of targets")
    targets = []
    for item in raw:
        try:
            node, class_type, field = str(item["node"]), item["class_type"], item["input"]
        except (KeyError, TypeError):
            raise WorkflowError(f"{where}: every target needs node, class_type and input") from None
        if node not in graph:
            raise WorkflowError(f"{where}: node {node} not in graph")
        actual = graph[node].get("class_type")
        if actual != class_type:
            raise WorkflowError(f"{where}: node {node} is {actual}, manifest says {class_type}")
        if field not in graph[node].get("inputs", {}):
            raise WorkflowError(f"{where}: node {node} ({class_type}) has no input {field!r}")
        targets.append(Target(node, class_type, field))
    return tuple(targets)


def _read_json(path: Path, what: str) -> tuple[Any, bytes]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise WorkflowError(f"{what} missing: {path.name}") from None
    try:
        return json.loads(data.decode("utf-8")), data
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{what} is not valid JSON: {exc}") from None


def _extra(pack_id: str, capability: Capability, name: str, raw: Any, graph: dict[str, Any]) -> Extra:
    where = f"{pack_id} extras.{name}"
    if name in capability.inputs:
        raise WorkflowError(f"{where}: clashes with the standard input of the same name")
    if not isinstance(raw, dict) or raw.get("type") not in EXTRA_TYPES:
        raise WorkflowError(f"{where}: needs a type in {list(EXTRA_TYPES)}")
    values = raw.get("values")
    if raw["type"] == "enum" and (not isinstance(values, dict) or not values):
        raise WorkflowError(f"{where}: an enum needs a non-empty values map")
    extra = Extra(
        name=name, type=raw["type"], description=raw.get("description", ""), default=raw.get("default"),
        targets=_targets(raw.get("bind"), graph, where), min=raw.get("min"), max=raw.get("max"),
        values=values if raw["type"] == "enum" else None,
    )
    try:
        default_raw = extra.convert(extra.default)
    except ParamError as exc:
        raise WorkflowError(f"{where}: default is invalid: {exc}") from None
    for target in extra.targets:
        current = graph[target.node]["inputs"][target.input]
        if not isinstance(current, list) and current != default_raw:
            raise WorkflowError(
                f"{where}: default {extra.default!r} does not match node {target.node}.{target.input} = {current!r}")
    return extra


def load_pack(directory: Path) -> WorkflowPack:
    directory = Path(directory)
    manifest, _ = _read_json(directory / "manifest.json", f"{directory.name}: manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise WorkflowError(f"{directory.name}: manifest schema must be {MANIFEST_SCHEMA!r}")
    pack_id = manifest.get("id")
    if pack_id != directory.name:
        raise WorkflowError(f"{directory.name}: manifest id {pack_id!r} must match the folder name")
    capability = CAPABILITIES.get(manifest.get("capability"))
    if capability is None:
        raise WorkflowError(f"{pack_id}: unknown capability {manifest.get('capability')!r}")
    graph, graph_bytes = _read_json(directory / manifest.get("graph", "workflow_api.json"), f"{pack_id}: graph")
    if not isinstance(graph, dict) or not graph or not all(
            isinstance(node, dict) and "class_type" in node for node in graph.values()):
        raise WorkflowError(f"{pack_id}: graph is not a ComfyUI API-format graph (use 'Export (API)' in ComfyUI)")

    inputs = {}
    for name, raw in (manifest.get("inputs") or {}).items():
        if name not in capability.inputs:
            raise WorkflowError(
                f"{pack_id}: {name!r} is not a standard {capability.name} input; declare it under extras")
        inputs[name] = _targets(raw, graph, f"{pack_id} inputs.{name}")
    missing = [name for name in capability.required_inputs if name not in inputs]
    if missing:
        raise WorkflowError(f"{pack_id}: required inputs not bound: {missing}")

    extras = {name: _extra(pack_id, capability, name, raw, graph)
              for name, raw in (manifest.get("extras") or {}).items()}

    outputs = {}
    for name, raw in (manifest.get("outputs") or {}).items():
        if name not in capability.outputs:
            raise WorkflowError(f"{pack_id}: {name!r} is not a standard {capability.name} output")
        try:
            node, class_type, key = str(raw["node"]), raw["class_type"], raw["key"]
        except (KeyError, TypeError):
            raise WorkflowError(f"{pack_id} outputs.{name}: needs node, class_type and key") from None
        if node not in graph:
            raise WorkflowError(f"{pack_id} outputs.{name}: node {node} not in graph")
        if graph[node].get("class_type") != class_type:
            raise WorkflowError(
                f"{pack_id} outputs.{name}: node {node} is {graph[node].get('class_type')}, manifest says {class_type}")
        outputs[name] = Output(name, node, class_type, key)
    missing = [name for name in capability.outputs if name not in outputs]
    if missing:
        raise WorkflowError(f"{pack_id}: outputs not declared: {missing}")

    return WorkflowPack(
        id=pack_id, capability=capability, description=manifest.get("description", ""),
        default=bool(manifest.get("default", False)), graph=graph,
        graph_sha256=hashlib.sha256(graph_bytes).hexdigest(), inputs=inputs, extras=extras,
        outputs=outputs, directory=directory,
    )


class WorkflowRegistry:
    """All workflow packs in a folder; packs that fail validation are listed in `errors`."""

    def __init__(self, packs: dict[str, WorkflowPack], errors: dict[str, str]):
        self.packs = packs
        self.errors = errors

    @classmethod
    def load(cls, root: Path = WORKFLOWS_DIR) -> "WorkflowRegistry":
        packs: dict[str, WorkflowPack] = {}
        errors: dict[str, str] = {}
        for directory in sorted(path for path in Path(root).iterdir() if path.is_dir()):
            if directory.name.startswith((".", "_")):
                continue
            try:
                pack = load_pack(directory)
            except WorkflowError as exc:
                errors[directory.name] = str(exc)
                continue
            packs[pack.id] = pack
        for capability in CAPABILITIES:
            defaults = sorted(p.id for p in packs.values() if p.capability.name == capability and p.default)
            if len(defaults) > 1:
                for pack_id in defaults:
                    errors[pack_id] = f"more than one default {capability} workflow: {defaults}"
                    del packs[pack_id]
        return cls(packs, errors)

    def get(self, capability: str, workflow_id: str | None = None) -> WorkflowPack:
        if workflow_id:
            pack = self.packs.get(workflow_id)
            if pack is None:
                reason = self.errors.get(workflow_id)
                detail = f": {reason}" if reason else f"; available: {sorted(self.packs)}"
                raise WorkflowError(f"workflow {workflow_id!r} is not available{detail}")
            if pack.capability.name != capability:
                raise WorkflowError(f"workflow {workflow_id!r} implements {pack.capability.name}, not {capability}")
            return pack
        candidates = sorted((p for p in self.packs.values() if p.capability.name == capability), key=lambda p: p.id)
        if not candidates:
            raise WorkflowError(f"no {capability} workflow is available")
        return next((p for p in candidates if p.default), candidates[0])
