"""Persistent clip-level deduplication ledger.

The ledger is intentionally keyed by the immutable Twitch clip ID, not by a
filename or title.  The Actions workflow restores and saves this file through
its durable cache so hourly runs share one history without enabling publishing
or requiring a database service.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DedupeStore:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "clips": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read dedupe ledger {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("clips", {}), dict):
            raise RuntimeError(f"invalid dedupe ledger format: {self.path}")
        data.setdefault("version", 1)
        return data

    def contains(self, clip_id: str) -> bool:
        record = self._data.get("clips", {}).get(str(clip_id))
        return bool(record and record.get("status") in {"prepared", "published"})

    def record(self, clip_id: str, status: str, **metadata: Any) -> None:
        self._data.setdefault("clips", {})[str(clip_id)] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **metadata,
        }
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + os.linesep,
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
