"""Job records: one folder per job under <output_dir>/<AssetName>/, never overwritten.

The folder keeps the downloaded outputs next to job.json, which records the prompt, seed,
parameters and workflow version, so every AI-generated asset stays traceable.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import JobNotFound, MetaOriError

JOB_SCHEMA = "metaori.job/1"
JOB_FILE = "job.json"
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def new_job_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class JobStore:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self._index: dict[str, Path] = {}

    def create(self, *, job_id: str, kind: str, asset_name: str,
               record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        folder = self.output_dir / asset_name / f"{_timestamp()}_{kind}_{job_id[:8]}"
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise MetaOriError(f"job folder {folder} already exists; refusing to overwrite it") from None
        full = {"schema": JOB_SCHEMA, "job_id": job_id, "asset_name": asset_name, "folder": str(folder),
                "created_at": now_iso(), **record}
        self.save(full)
        return folder, full

    def save(self, record: dict[str, Any]) -> None:
        folder = Path(record["folder"])
        part = folder / (JOB_FILE + ".part")
        part.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(part, folder / JOB_FILE)
        self._index[record["job_id"]] = folder

    def load(self, job_id: str) -> dict[str, Any]:
        try:
            canonical = str(uuid.UUID(str(job_id)))
        except ValueError:
            raise JobNotFound(f"{job_id!r} is not a MetaOri job id") from None
        folder = self._index.get(canonical) or self._scan(canonical)
        if folder is None:
            raise JobNotFound(f"no MetaOri job {canonical} under {self.output_dir}")
        record = json.loads((folder / JOB_FILE).read_text(encoding="utf-8"))
        self._index[canonical] = folder
        return record

    def _scan(self, job_id: str) -> Path | None:
        if not self.output_dir.is_dir():
            return None
        for path in self.output_dir.glob(f"*/*_{job_id[:8]}/{JOB_FILE}"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("job_id") == job_id:
                    return path.parent
            except (OSError, ValueError):
                continue
        return None
