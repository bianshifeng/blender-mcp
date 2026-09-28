"""MetaOri configuration, read from environment variables."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"


@dataclass(frozen=True)
class MetaOriConfig:
    comfyui_url: str
    output_dir: Path
    export_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> "MetaOriConfig":
        """Build the config; relative directories resolve against cwd (default: process cwd)."""
        env = os.environ if env is None else env
        base = Path(cwd) if cwd is not None else Path.cwd()

        def directory(name: str, default: Path) -> Path:
            raw = (env.get(name) or "").strip()
            path = Path(raw).expanduser() if raw else default
            return (path if path.is_absolute() else base / path).resolve()

        url = (env.get("METAORI_COMFYUI_URL") or "").strip() or DEFAULT_COMFYUI_URL
        home = Path.home() / "MetaOri"
        return cls(
            comfyui_url=url.rstrip("/"),
            output_dir=directory("METAORI_OUTPUT_DIR", home / "AI_Raw"),
            export_dir=directory("METAORI_EXPORT_DIR", home / "Export"),
        )
