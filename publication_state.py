"""Persistent publication state and analytics-ready records.

This ledger is separate from the prepared-clip dedupe cache so publishing can
track each network independently.  It never treats an HTTP request as a
successful publication; the network status must be confirmed separately.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATES = {"QUEUED", "PUBLISHING"}
SUCCESS_STATES = {"PUBLISHED"}
PIPELINE_STATES = {
    "DISCOVERED",
    "ELIGIBLE",
    "ACQUIRED",
    "RENDERED",
    "QC_PASSED",
    "QUEUED",
    "PUBLISHING",
    "PUBLISHED",
    "FAILED",
    "SKIPPED",
}


class PublicationLedger:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "clips": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read publication ledger {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("clips", {}), dict):
            raise RuntimeError(f"invalid publication ledger format: {self.path}")
        data.setdefault("version", 1)
        return data

    def get(self, clip_id: str) -> dict[str, Any] | None:
        return self._data.get("clips", {}).get(str(clip_id))

    def is_published(self, clip_id: str) -> bool:
        record = self.get(clip_id) or {}
        if record.get("status") in SUCCESS_STATES:
            return True
        return any(
            network.get("status") in SUCCESS_STATES
            for network in (record.get("networks") or {}).values()
        )

    def is_active(self, clip_id: str) -> bool:
        record = self.get(clip_id) or {}
        if record.get("status") in ACTIVE_STATES:
            return True
        return any(
            network.get("status") in ACTIVE_STATES
            for network in (record.get("networks") or {}).values()
        )

    def upsert_clip(self, clip_id: str, status: str, **metadata: Any) -> dict[str, Any]:
        if status not in PIPELINE_STATES:
            raise ValueError(f"invalid publication state: {status}")
        record = dict(self.get(clip_id) or {})
        record.update(metadata)
        record["clip_id"] = str(clip_id)
        record["status"] = status
        timestamp = datetime.now(timezone.utc).isoformat()
        record["updated_at"] = timestamp
        history = list(record.get("state_history") or [])
        if not history or history[-1].get("status") != status:
            history.append({"status": status, "at": timestamp})
        record["state_history"] = history
        record.setdefault("networks", {})
        record.setdefault("analytics", {
            "views": None,
            "likes": None,
            "comments": None,
            "shares": None,
            "reach": None,
            "watch_time": None,
            "completion_rate": None,
            "follows_generated": None,
        })
        self._data.setdefault("clips", {})[str(clip_id)] = record
        self._write()
        return record

    def update_network(self, clip_id: str, network: str, status: str, **metadata: Any) -> dict[str, Any]:
        if status not in PIPELINE_STATES:
            raise ValueError(f"invalid publication state: {status}")
        record = dict(self.get(clip_id) or {"clip_id": str(clip_id), "networks": {}})
        networks = dict(record.get("networks") or {})
        network_record = dict(networks.get(network) or {})
        network_record.update(metadata)
        network_record["status"] = status
        timestamp = datetime.now(timezone.utc).isoformat()
        network_record["updated_at"] = timestamp
        history = list(network_record.get("state_history") or [])
        if not history or history[-1].get("status") != status:
            history.append({"status": status, "at": timestamp})
        network_record["state_history"] = history
        networks[network] = network_record
        record["networks"] = networks
        record["updated_at"] = timestamp
        self._data.setdefault("clips", {})[str(clip_id)] = record
        self._write()
        return record

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + os.linesep,
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
