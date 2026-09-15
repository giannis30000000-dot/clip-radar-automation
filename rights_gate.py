"""Clip Radar viral-first rights gate.

Rule: rank for viral potential first, then accept the highest-ranked candidate whose
broadcaster has been verified as viewer-social-sharing enabled or explicitly licensed.
If #1 is not eligible, continue #2, #3, etc. Never publish an unverified source.
"""
import json
from pathlib import Path

WHITELIST = Path(__file__).with_name("sharing_whitelist.json")


def load_verified():
    data=json.loads(WHITELIST.read_text(encoding="utf-8"))
    return {k.lower():v for k,v in data.get("verified",{}).items() if v.get("viewer_social_sharing") is True}


def eligible_broadcaster(candidate):
    broadcaster=(candidate.get("streamer") or candidate.get("broadcaster_name") or candidate.get("broadcaster") or "").strip().lower()
    return broadcaster if broadcaster in load_verified() else None


def pick_eligible(ranked_candidates, limit=4):
    verified=load_verified(); selected=[]; skipped=[]
    for c in ranked_candidates:
        broadcaster=(c.get("streamer") or c.get("broadcaster_name") or c.get("broadcaster") or "").strip().lower()
        if broadcaster in verified:
            selected.append(c)
            if len(selected)>=limit: break
        else:
            skipped.append({"candidate":c,"reason":"sharing_not_verified","broadcaster":broadcaster})
    return selected, skipped
