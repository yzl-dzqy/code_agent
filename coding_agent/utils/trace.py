# -*- coding: utf-8 -*-
"""JSONL trace recorder for agent runs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TraceRecorder:
    """Append structured trace events to a JSONL file."""

    def __init__(self, path: Path, *, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def record(self, event: str, **data: Any) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
