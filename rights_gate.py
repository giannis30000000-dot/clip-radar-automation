"""Clip Radar viral-first rights gate.

Rule: rank for viral potential first, then accept the highest-ranked candidate whose
broadcaster has been verified as viewer-social-sharing enabled or explicitly licensed.
If #1 is not eligible, continue #2, #3, etc. Never publish an unverified source.
"""
import json
from pathlib import Path

WHITELIST = Path(__file__).with_name("sharing_whitelist.json")
ACCEPTED_RIGHTS_BASES = {
    "VERIFIED_TWITCH_SOCIAL_SHARING",
    "EXPLICIT_CREATOR_PERMISSION",
    "TEAM_OR_RIGHTSHOLDER_PERMISSION",
    "LICENSED_PROVIDER",
    "OTHER_VERIFIED_COMMERCIAL_LICENSE",
}


def load_verified():
    data=json.loads(WHITELIST.read_text(encoding="utf-8"))
    return {k.lower():v for k,v in data.get("verified",{}).items() if v.get("viewer_social_sharing") is True}


def rights_evidence(candidate):
    broadcaster=(candidate.get("streamer") or candidate.get("broadcaster_name") or candidate.get("broadcaster") or "").strip().lower()
    entry=load_verified().get(broadcaster)
    if not entry:
        return {"status":"REVIEW_REQUIRED","reason":"sharing_not_verified","broadcaster":broadcaster}
    basis=entry.get("rights_basis") or "VERIFIED_TWITCH_SOCIAL_SHARING"
    if basis not in ACCEPTED_RIGHTS_BASES:
        return {"status":"REVIEW_REQUIRED","reason":"unknown_rights_basis","broadcaster":broadcaster,"rights_basis":basis}
    return {"status":"RIGHTS_VERIFIED","reason":"documented_verified_broadcaster_entry","broadcaster":broadcaster,"rights_basis":basis,"verified_via":entry.get("verified_via")}


def eligible_broadcaster(candidate):
    evidence=rights_evidence(candidate)
    return evidence["broadcaster"] if evidence["status"] == "RIGHTS_VERIFIED" else None


def pick_eligible(ranked_candidates, limit=4):
    verified=load_verified(); selected=[]; skipped=[]
    for c in ranked_candidates:
        evidence=rights_evidence(c)
        if evidence["status"] == "RIGHTS_VERIFIED":
            selected.append(c)
            if len(selected)>=limit: break
        else:
            skipped.append({"candidate":c,"reason":evidence["reason"],"broadcaster":evidence["broadcaster"]})
    return selected, skipped
