# mini-projects/04-chief-of-staff-router/queue_manager.py
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

QUEUES = ("research", "write", "tool", "review")


def enqueue(root: Path, destination: str, job: Mapping[str, Any]) -> Path:
    if destination not in QUEUES:
        raise ValueError(f"unknown queue {destination!r}")
    folder = root / destination
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{uuid.uuid4().hex}.json"
    # Atomic write: a worker never sees a half-written job.
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, default=str)
        os.replace(tmp, final)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return final