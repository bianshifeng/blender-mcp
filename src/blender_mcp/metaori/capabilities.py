"""Capability contracts: the standard inputs and outputs every workflow of a capability implements.

Also holds the asset-name rule and the purpose presets that shape text-to-image prompts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ParamError

ASSET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")
ASSET_NAME_MAX = 64
MAX_SEED = 2**53 - 1
PRESETS_PATH = Path(__file__).with_name("presets.json")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: type
    description: str
    min: int | None = None
    max: int | None = None
    multiple_of: int | None = None
    choices: tuple[int, ...] | None = None
    max_length: int | None = None

    def validate(self, value: Any) -> Any:
        if self.type is bool:
            if not isinstance(value, bool):
                raise ParamError(f"{self.name} must be true or false, got {value!r}")
            return value
        if self.type is str:
            if not isinstance(value, str):
                raise ParamError(f"{self.name} must be a string, got {value!r}")
            if self.max_length is not None and len(value) > self.max_length:
                raise ParamError(f"{self.name} is longer than {self.max_length} characters")
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParamError(f"{self.name} must be an integer, got {value!r}")
        if self.min is not None and value < self.min:
            raise ParamError(f"{self.name} must be >= {self.min}, got {value}")
        if self.max is not None and value > self.max:
            raise ParamError(f"{self.name} must be <= {self.max}, got {value}")
        if self.multiple_of and value % self.multiple_of:
            raise ParamError(f"{self.name} must be a multiple of {self.multiple_of}, got {value}")
        if self.choices is not None and value not in self.choices:
            raise ParamError(f"{self.name} must be one of {list(self.choices)}, got {value}")
        return value


@dataclass(frozen=True)
class Capability:
    name: str
    kind: str
    inputs: dict[str, ParamSpec]
    required_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    internal_inputs: tuple[str, ...] = ()

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check standard parameters; None values are dropped (meaning "not given")."""
        unknown = sorted(set(params) - set(self.inputs))
        if unknown:
            raise ParamError(f"unknown {self.name} parameters: {unknown}")
        return {key: self.inputs[key].validate(value) for key, value in params.items() if value is not None}


def _seed() -> ParamSpec:
    return ParamSpec("seed", int, "Sampler seed; random when omitted", min=0, max=MAX_SEED)


def _prefix() -> ParamSpec:
    return ParamSpec("filename_prefix", str, "ComfyUI output prefix, set by MetaOri", max_length=200)


TEXT_TO_IMAGE = Capability(
    name="text_to_image",
    kind="t2i",
    inputs={
        "prompt": ParamSpec("prompt", str, "Positive prompt", max_length=4000),
        "negative_prompt": ParamSpec("negative_prompt", str, "Negative prompt", max_length=4000),
        "width": ParamSpec("width", int, "Image width in pixels", min=256, max=2048, multiple_of=8),
        "height": ParamSpec("height", int, "Image height in pixels", min=256, max=2048, multiple_of=8),
        "seed": _seed(),
        "count": ParamSpec("count", int, "Images per job", min=1, max=4),
        "filename_prefix": _prefix(),
    },
    required_inputs=("prompt",),
    outputs=("images",),
    internal_inputs=("filename_prefix",),
)

IMAGE_TO_3D = Capability(
    name="image_to_3d",
    kind="i2m",
    inputs={
        "image": ParamSpec("image", str, "ComfyUI input image name, set by MetaOri after upload", max_length=500),
        "seed": _seed(),
        "target_faces": ParamSpec("target_faces", int, "Face count after decimation", min=1000, max=2_000_000),
        "texture_size": ParamSpec("texture_size", int, "Baked texture resolution", choices=(512, 1024, 2048, 4096)),
        "remove_background": ParamSpec("remove_background", bool, "Remove the image background first"),
        "filename_prefix": _prefix(),
    },
    required_inputs=("image",),
    outputs=("model",),
    internal_inputs=("image", "filename_prefix"),
)

CAPABILITIES = {capability.name: capability for capability in (TEXT_TO_IMAGE, IMAGE_TO_3D)}


def validate_asset_name(name: Any) -> str:
    if not isinstance(name, str) or len(name) > ASSET_NAME_MAX or not ASSET_NAME_RE.match(name):
        raise ParamError(
            "asset_name must be PascalCase letters and digits with optional _Variant parts "
            f"(e.g. BronzeCenser or WoodCrate_A), at most {ASSET_NAME_MAX} characters; got {name!r}"
        )
    return name


@dataclass(frozen=True)
class PromptPreset:
    name: str
    description: str
    positive_suffix: str
    negative: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class ComposedPrompt:
    positive: str
    negative: str | None  # None leaves the workflow's own negative prompt in place


def load_presets(path: Path = PRESETS_PATH) -> dict[str, PromptPreset]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        name: PromptPreset(
            name=name,
            description=spec.get("description", ""),
            positive_suffix=spec.get("positive_suffix", ""),
            negative=spec.get("negative", ""),
            width=spec.get("width"),
            height=spec.get("height"),
        )
        for name, spec in data["purposes"].items()
    }


def compose_prompt(preset: PromptPreset, prompt: str, negative_prompt: str | None = None) -> ComposedPrompt:
    """Append the preset to the caller's prompt; the negative is preset + caller, comma-joined."""
    text = (prompt or "").strip()
    if not text:
        raise ParamError("prompt must not be empty")
    positive = f"{text}, {preset.positive_suffix}" if preset.positive_suffix else text
    parts = [part.strip() for part in (preset.negative, negative_prompt or "") if part and part.strip()]
    return ComposedPrompt(positive=positive, negative=", ".join(parts) if parts else None)
