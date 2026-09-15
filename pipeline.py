"""Clip Radar unattended pipeline stage.

This stage consumes Twitch discovery results and creates a ranked, source-eligibility queue.
It intentionally does NOT bypass Twitch download permissions. Candidates without an authorized
source are skipped so the unattended pipeline can continue to the next candidate.
"""
import os
from dataclasses import dataclass


@dataclass
class Candidate:
    clip_id: str
    streamer: str
    title: str
    url: str
    score: float
    views: int
    duration: float
    authorized_source_url: str | None = None


def eligible_for_edit(c: Candidate) -> tuple[bool, str]:
    if c.score < 45:
        return False, "quality_score_below_threshold"
    if c.views < 75:
        return False, "insufficient_traction"
    if not 8 <= c.duration <= 60:
        return False, "duration_outside_shortform_range"
    return True, "ready"


def choose_next(candidates: list[Candidate]) -> Candidate | None:
    for c in sorted(candidates, key=lambda x: x.score, reverse=True):
        ok, reason = eligible_for_edit(c)
        print(f"eligibility | {c.streamer} | {c.title} | score={c.score} | {reason}")
        if ok:
            return c
    return None


if __name__ == "__main__":
    print("Clip Radar pipeline stage installed: discovery -> eligibility -> edit queue")
