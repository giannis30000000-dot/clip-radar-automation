"""Reconcile known Buffer results after an interrupted live orchestration run."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from publication_state import PublicationLedger


class ReconciliationError(RuntimeError):
    """Raised when a reconciliation would be ambiguous or unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile(
    entries: list[dict[str, Any]],
    missing_networks: tuple[str, ...] = ("instagram",),
    state_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    state_path = state_path or Path(os.getenv("CLIP_RADAR_PUBLICATION_STATE_FILE", "state/publications.json"))
    output_path = output_path or Path(os.getenv("CLIP_RADAR_OUTPUT_DIR", "output")) / "buffer_reconciliation.json"
    ledger = PublicationLedger(state_path)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        clip_id = str(entry.get("clip_id") or "")
        network = str(entry.get("network") or "").strip().lower()
        post_id = str(entry.get("buffer_post_id") or "")
        status = str(entry.get("status") or "").strip().upper()
        if not clip_id or network not in {"instagram", "tiktok"} or not post_id:
            raise ReconciliationError("each entry requires clip_id, Buffer post ID, and Instagram/TikTok network")
        if status not in {"PUBLISHED", "QUEUED", "PUBLISHING"}:
            raise ReconciliationError(f"unsupported reconciled Buffer status: {status}")
        key = (clip_id, network)
        if key in seen:
            raise ReconciliationError(f"duplicate reconciliation entry: {clip_id}/{network}")
        seen.add(key)
        record = ledger.get(clip_id) or {}
        if not record:
            raise ReconciliationError(f"publication ledger has no record for {clip_id}")
        existing = (record.get("networks") or {}).get(network) or {}
        if existing.get("buffer_post_id") and existing.get("buffer_post_id") != post_id:
            raise ReconciliationError(f"conflicting Buffer post ID for {clip_id}/{network}")
        ledger.update_network(
            clip_id,
            network,
            status,
            buffer_post_id=post_id,
            channel_id=str(entry.get("channel_id") or ""),
            due_at=entry.get("due_at"),
            reconciled_at=_now(),
            reconciliation_basis="read_only_buffer_audit_after_orchestrator_interrupt",
        )
        for missing in missing_networks:
            missing = str(missing).strip().lower()
            if missing not in {"instagram", "tiktok"} or missing == network:
                continue
            missing_record = (ledger.get(clip_id) or {}).get("networks", {}).get(missing) or {}
            if missing_record.get("status") in {"QUEUED", "PUBLISHING", "PUBLISHED"}:
                continue
            ledger.update_network(
                clip_id,
                missing,
                "FAILED",
                channel_id=str(entry.get(f"{missing}_channel_id") or ""),
                error="network result was absent from the interrupted run; retry only this network",
                reconciled_at=_now(),
                reconciliation_basis="read_only_buffer_audit_missing_network_result",
            )
        ledger.upsert_clip(clip_id, "PUBLISHING", buffer_reconciliation_at=_now())
        results.append(
            {
                "clip_id": clip_id,
                "network": network,
                "buffer_post_id": post_id,
                "status": status,
                "missing_networks_marked_retryable": [
                    missing for missing in missing_networks if missing != network
                ],
            }
        )
    result = {
        "schema_version": 1,
        "status": "READY",
        "results": results,
        "entries": len(results),
        "credential_names": [],
        "reconciled_at": _now(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    raw = os.getenv("BUFFER_RECONCILIATION_JSON", "")
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError("mapping must be a JSON list")
        result = reconcile(entries)
    except (ValueError, TypeError, json.JSONDecodeError, ReconciliationError) as exc:
        print(f"buffer_reconcile | BLOCKED | reason={exc}")
        raise SystemExit(2) from exc
    print(f"buffer_reconcile | READY | entries={result['entries']}")
