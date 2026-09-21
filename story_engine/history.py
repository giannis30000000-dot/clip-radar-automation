"""Atomic JSON history, unique premise reservations, portable cloud state."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .schema import concept_fingerprint


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class HistoryBusy(RuntimeError):
    pass


class DuplicatePremise(RuntimeError):
    pass


class StoryHistory:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise HistoryBusy("Another story generation owns the history lock; retry when it finishes.") from exc
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(str(os.getpid()))
            yield self
        finally:
            lock.unlink(missing_ok=True)

    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "stories": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("stories"), dict):
            raise ValueError("invalid story history; refusing to discard dedupe")
        return data

    def fingerprints(self) -> set[str]:
        return {v["concept_fingerprint"] for v in self.read()["stories"].values()}

    def reserve(self, story: dict, output: Path) -> None:
        data = self.read()
        words = set(re.findall(r"[a-z0-9]+", story["concept"].lower()))
        fingerprint = concept_fingerprint(story["concept"])
        for previous in data["stories"].values():
            old_words = set(re.findall(r"[a-z0-9]+", previous["concept"].lower()))
            similarity = len(words & old_words) / max(1, len(words | old_words))
            if previous["concept_fingerprint"] == fingerprint or similarity >= 0.8:
                raise DuplicatePremise("This premise or a near-identical wording is already in story history.")
        if story["story_id"] in data["stories"]:
            raise DuplicatePremise("story_id already exists")
        data["stories"][story["story_id"]] = {
            "story_id": story["story_id"], "title": story["title"], "concept": story["concept"],
            "concept_fingerprint": fingerprint, "characters": story["characters"],
            "date_generated": datetime.now(timezone.utc).isoformat(), "output_path": str(output),
            "status": "GENERATING", "qc_result": None, "publication_state": "REVIEW_REQUIRED",
            "analytics": {}, "generation_attempts": 1,
        }
        write_json(self.path, data)

    def update(self, story_id: str, **fields) -> None:
        data = self.read()
        data["stories"][story_id].update(fields)
        write_json(self.path, data)
