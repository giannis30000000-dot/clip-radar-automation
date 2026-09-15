"""Conservative third-party media screening before Clip Radar publication."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


REVIEW_PATTERNS = (
    (r"\b(?:movie|film|television|tv show|tv episode|series episode)\b", "movie_or_tv_reference"),
    (r"\b(?:music video|official video|official song|copyrighted song)\b", "music_video_or_song"),
    (r"\b(?:sports broadcast|game broadcast|tournament broadcast|esports broadcast)\b", "broadcast_footage"),
    (r"\b(?:rebroadcast|re-broadcast|watch party|react(?:s|ing)? to (?:a )?(?:video|stream|movie))\b", "rebroadcast_or_reaction_media"),
    (r"\b(?:trailer|highlight package|full match|full episode)\b", "substantial_third_party_media"),
)


def check_third_party_content(
    candidate: Mapping[str, Any],
    transcript_entries: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    transcript = " ".join(str(item.get("text") or "") for item in (transcript_entries or []))
    text = " ".join(
        str(candidate.get(field) or "")
        for field in ("title", "game_name", "category", "description")
    ) + " " + transcript
    for pattern, reason in REVIEW_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return {
                "status": "REVIEW_REQUIRED",
                "reason": reason,
                "matched_pattern": pattern,
            }
    return {
        "status": "THIRD_PARTY_CHECK_PASSED",
        "reason": "no_obvious_unlicensed_third_party_media_signal",
    }
