"""Shared types for pluggable Clip Radar publication backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ValidatedClip:
    candidate: Mapping[str, Any]
    final_path: Path
    qc_status: str
    rights_basis: str = "VERIFIED_TWITCH_SOCIAL_SHARING"
    third_party_check: str = "THIRD_PARTY_CHECK_PASSED"
    transformation_check: str = "TRANSFORMATION_CHECK_PASSED"
