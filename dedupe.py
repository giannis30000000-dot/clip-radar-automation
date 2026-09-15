"""Persistent multi-layer Clip Radar deduplication ledger.

The ledger is deliberately independent of filenames. It remembers exact Twitch
IDs, source-moment identity, publication-history aliases, and compact
audio/visual fingerprints. GitHub Actions restores this JSON through its
cache, so the same rules apply across unrelated cloud runners.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fingerprint import fingerprint_similarity, video_fingerprint

_STOP_WORDS = {
    "a", "an", "and", "are", "gets", "got", "has", "in", "is", "it", "just",
    "of", "on", "the", "to", "xqc", "with", "from", "this", "that",
}
_CLIP_URL_RE = re.compile(r"/clip/([A-Za-z0-9_-]+)")


def _tokens(value: Any) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    return {word for word in words if word not in _STOP_WORDS and len(word) > 1}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def source_moment(candidate: Any) -> dict[str, Any]:
    """Normalize the VOD/timestamp identity available from the Twitch API."""

    def value(name: str, default: Any = None) -> Any:
        if isinstance(candidate, dict):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    broadcaster = str(value("broadcaster_login") or value("streamer") or value("broadcaster_name") or "").strip().lower()
    video_id = str(value("video_id") or value("vod_id") or value("videoId") or "").strip()
    raw_offset = value("vod_offset") or value("offset") or value("start_offset") or value("startOffset")
    try:
        offset = round(float(raw_offset), 3) if raw_offset is not None else None
    except (TypeError, ValueError):
        offset = None
    created = _parse_time(value("created_at") or value("createdAt"))
    try:
        duration = round(float(value("duration") or 0), 3)
    except (TypeError, ValueError):
        duration = 0.0
    result = {
        "broadcaster": broadcaster,
        "broadcaster_id": str(value("broadcaster_id") or "").strip(),
        "video_id": video_id,
        "offset": offset,
        "created_at": created.isoformat() if created else "",
        "duration": duration,
        "title_tokens": sorted(_tokens(value("title") or "")),
    }
    if video_id and offset is not None:
        result["key"] = f"{broadcaster}|vod:{video_id}|offset:{round(offset, 1):.1f}"
    elif video_id and created:
        result["key"] = f"{broadcaster}|vod:{video_id}|created:{int(created.timestamp() // 30)}"
    elif created:
        result["key"] = f"{broadcaster}|created:{int(created.timestamp() // 20)}"
    else:
        result["key"] = ""
    return result


def same_source_moment(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return true only for a plausible duplicate moment, not just a shared title."""

    if not left or not right or left.get("broadcaster") != right.get("broadcaster"):
        return False
    left_video, right_video = left.get("video_id"), right.get("video_id")
    if left_video and right_video and left_video != right_video:
        return False
    left_offset, right_offset = left.get("offset"), right.get("offset")
    left_duration, right_duration = float(left.get("duration") or 0), float(right.get("duration") or 0)
    if left_offset is not None and right_offset is not None:
        return abs(float(left_offset) - float(right_offset)) <= max(left_duration, right_duration, 8.0) + 5.0
    left_created, right_created = _parse_time(left.get("created_at")), _parse_time(right.get("created_at"))
    if not left_created or not right_created or abs((left_created - right_created).total_seconds()) > 45:
        return False
    overlap = len(set(left.get("title_tokens") or []) & set(right.get("title_tokens") or []))
    return overlap >= 2 or bool(left_video and right_video)


def _history_broadcaster(record: dict[str, Any]) -> str:
    value = record.get("broadcaster") or record.get("streamer") or ""
    if value:
        return str(value).strip().lower()
    tags = re.findall(r"#([A-Za-z0-9_]+)", str(record.get("text") or ""))
    known = {"xqc", "kaicenat", "jynxzi", "caseoh_", "stableronaldo", "zackrawrr", "sodapoppin", "lacy"}
    return next((tag.lower() for tag in tags if tag.lower() in known), "")


class DedupeStore:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._read()
        self._load_seed_history()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 2, "clips": {}, "history": {}, "fingerprints": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read dedupe ledger {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("clips", {}), dict):
            raise RuntimeError(f"invalid dedupe ledger format: {self.path}")
        data.setdefault("version", 2)
        data.setdefault("history", {})
        data.setdefault("fingerprints", {})
        return data

    def _load_seed_history(self) -> None:
        seed_path = Path(__file__).with_name("historical_publications.json")
        if not seed_path.exists():
            return
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid historical publication seed: {seed_path}") from exc
        changed = False
        for item in seed.get("publications", []):
            key = str(item.get("record_key") or item.get("post_id") or item.get("clip_id") or "").strip()
            if not key:
                continue
            record = {k: v for k, v in item.items() if k != "record_key"}
            record.setdefault("status", "PUBLISHED")
            record.setdefault("source", "historical_seed")
            if self._data.setdefault("history", {}).get(key) != record:
                self._data["history"][key] = record
                changed = True
            clip_id = record.get("clip_id")
            if clip_id and str(clip_id) not in self._data.setdefault("clips", {}):
                self._data["clips"][str(clip_id)] = {
                    "status": "published",
                    "updated_at": record.get("published_at") or datetime.now(timezone.utc).isoformat(),
                    "broadcaster": record.get("broadcaster", ""),
                    "title": record.get("title", ""),
                    "source_moment": record.get("source_moment", {}),
                    "historical_post_id": record.get("post_id", ""),
                }
                changed = True
        if changed:
            self._write()

    def contains(self, clip_id: str) -> bool:
        record = self._data.get("clips", {}).get(str(clip_id))
        return bool(record and record.get("status") in {"prepared", "published"})

    def find_duplicate(
        self,
        candidate: dict[str, Any],
        source_path: Path | None = None,
        final_path: Path | None = None,
        source_fingerprint: dict[str, Any] | None = None,
        final_fingerprint: dict[str, Any] | None = None,
        ignore_exact: bool = False,
    ) -> dict[str, Any] | None:
        """Find an existing clip by ID, moment, historical alias, or fingerprint."""

        clip_id = str(candidate.get("id") or candidate.get("clip_id") or "")
        exact = self._data.get("clips", {}).get(clip_id)
        if exact and exact.get("status") in {"prepared", "published"} and not ignore_exact:
            return {"reason": "exact_clip_id", "clip_id": clip_id, "record": exact}
        if exact and exact.get("status") in {"prepared", "published"} and ignore_exact:
            # A failed-network retry is allowed to reprocess its own exact ID;
            # do not let the historical alias for that same post block it.
            return None
        moment = source_moment(candidate)
        for existing_id, record in (self._data.get("clips") or {}).items():
            if existing_id == clip_id or record.get("status") not in {"prepared", "published"}:
                continue
            if same_source_moment(moment, record.get("source_moment") or {}):
                return {"reason": "same_source_moment", "clip_id": existing_id, "record": record}
        candidate_tokens = _tokens(candidate.get("title"))
        broadcaster = str(candidate.get("streamer") or candidate.get("broadcaster_name") or "").strip().lower()
        if len(candidate_tokens) >= 3:
            for history_id, record in (self._data.get("history") or {}).items():
                if _history_broadcaster(record) != broadcaster:
                    continue
                history_tokens = _tokens(record.get("title") or record.get("text"))
                overlap = len(candidate_tokens & history_tokens)
                if overlap >= 3 and overlap / max(1, len(candidate_tokens | history_tokens)) >= 0.35:
                    return {"reason": "publication_history_alias", "history_id": history_id, "record": record}
        source_fingerprint = source_fingerprint or self._fingerprint_if_real(source_path)
        final_fingerprint = final_fingerprint or self._fingerprint_if_real(final_path)
        if source_fingerprint or final_fingerprint:
            for existing_id, record in (self._data.get("clips") or {}).items():
                if record.get("status") not in {"prepared", "published"} or existing_id == clip_id:
                    continue
                for label, incoming in (("source", source_fingerprint), ("final", final_fingerprint)):
                    if not incoming:
                        continue
                    for key in (f"{label}_fingerprint", "fingerprint"):
                        previous = record.get(key)
                        if not previous:
                            continue
                        comparison = fingerprint_similarity(incoming, previous)
                        if comparison["duplicate"]:
                            return {"reason": f"perceptual_{label}_fingerprint", "clip_id": existing_id, "similarity": comparison, "record": record}
        return None

    @staticmethod
    def _fingerprint_if_real(path: Path | None) -> dict[str, Any] | None:
        if not path or not path.exists() or path.stat().st_size < 50_000:
            return None
        return video_fingerprint(path)

    def record(self, clip_id: str, status: str, **metadata: Any) -> None:
        self._data.setdefault("clips", {})[str(clip_id)] = {
            "status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **metadata,
        }
        if metadata.get("source_fingerprint"):
            self._data.setdefault("fingerprints", {})[str(clip_id)] = {
                "source": metadata["source_fingerprint"], "final": metadata.get("final_fingerprint"),
            }
        self._write()

    def import_publication_history(self, posts: Iterable[dict[str, Any]], source: str = "buffer_audit") -> dict[str, int]:
        """Import read-only publication evidence without making network writes."""

        imported = 0
        for post in posts:
            post_id = str(post.get("id") or post.get("post_id") or "").strip()
            if not post_id:
                continue
            record = {
                "post_id": post_id, "channel_id": str(post.get("channel_id") or ""),
                "channel_name": str(post.get("channel_name") or ""), "service": str(post.get("service") or ""),
                "status": str(post.get("status") or "UNKNOWN").upper(), "created_at": post.get("created_at"),
                "due_at": post.get("due_at"), "text": str(post.get("text") or "")[:500], "source": source,
            }
            match = _CLIP_URL_RE.search(record["text"])
            if match:
                record["clip_id"] = match.group(1)
            record["broadcaster"] = _history_broadcaster(record)
            self._data.setdefault("history", {})[post_id] = record
            imported += 1
            if record.get("clip_id"):
                clip_id = str(record["clip_id"])
                existing = self._data.setdefault("clips", {}).get(clip_id)
                if not existing or existing.get("status") not in {"prepared", "published"}:
                    self._data["clips"][clip_id] = {
                        "status": "published" if record["status"] in {"SENT", "PUBLISHED"} else "prepared",
                        "updated_at": record.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        "broadcaster": record.get("broadcaster", ""), "title": record.get("text", "")[:160],
                        "historical_post_id": post_id,
                    }
        if imported:
            self._write()
        return {"imported": imported, "history_records": len(self._data.get("history") or {})}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(self._data, indent=2, sort_keys=True) + os.linesep, encoding="utf-8")
        os.replace(temporary, self.path)
