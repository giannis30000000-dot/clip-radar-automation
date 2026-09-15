import os
import math
import re
import requests
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
STREAMERS = [s.strip() for s in os.getenv("STREAMERS", "xqc,kaicenat,jynxzi,caseoh_").split(",") if s.strip()]


def get_app_token():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET")
    r = requests.post("https://id.twitch.tv/oauth2/token", params={"client_id": TWITCH_CLIENT_ID,"client_secret": TWITCH_CLIENT_SECRET,"grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token):
    return {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}


def get_user_ids(token, names):
    r = requests.get("https://api.twitch.tv/helix/users", headers=headers(token), params=[("login", n) for n in names], timeout=20)
    r.raise_for_status()
    return {u["login"].lower(): u for u in r.json().get("data", [])}


def get_clips(token, broadcaster_id, started_at, ended_at):
    params = {"broadcaster_id": broadcaster_id,"started_at": started_at.isoformat().replace("+00:00", "Z"),"ended_at": ended_at.isoformat().replace("+00:00", "Z"),"first": 100}
    clips = []
    for _ in range(3):
        r = requests.get("https://api.twitch.tv/helix/clips", headers=headers(token), params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        clips.extend(body.get("data", []))
        cursor = body.get("pagination", {}).get("cursor")
        if not cursor:
            break
        params["after"] = cursor
    return clips


def get_games(token, game_ids):
    ids = [game_id for game_id in sorted(set(game_ids)) if game_id]
    if not ids:
        return {}
    r = requests.get("https://api.twitch.tv/helix/games", headers=headers(token), params=[("id", game_id) for game_id in ids[:100]], timeout=20)
    r.raise_for_status()
    return {game["id"]: game["name"] for game in r.json().get("data", [])}


def clip_stats(clip, now):
    created = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    age_h = max(0.25, (now-created).total_seconds()/3600)
    views = int(clip.get("view_count", 0))
    return created, age_h, views, views/age_h


def total_score(clip, now):
    _, age_h, views, vph = clip_stats(clip, now)
    # Fresh is useful, but never enough by itself.
    freshness = max(0, 100-age_h*4)
    velocity = min(100, 22*math.log10(vph+1))
    traction = min(100, 24*math.log10(views+1))
    duration = float(clip.get("duration", 0))
    duration_score = 100 if 12 <= duration <= 55 else (70 if 8 <= duration <= 60 else 25)
    title=(clip.get("title") or "").lower()
    hook_words=["wtf","crazy","no way","delivery","rage","funny","reaction","insane","caught","reveals","gets","irl","fight","shocked","package","clutch","ace","fails","roast","drama","screams","scream","dies","win","loss"]
    hook=min(100, sum(18 for k in hook_words if k in title))
    category = (clip.get("game_name") or "").lower()
    gaming_momentum = 12 if category else 0
    # Penalize near-zero traction heavily so brand-new 4-view clips do not outrank proven moments.
    low_penalty = 35 if views < 20 else (18 if views < 75 else 0)
    return round(0.22*freshness + 0.31*velocity + 0.25*traction + 0.12*duration_score + 0.08*hook + 0.02*gaming_momentum - low_penalty, 2)


def normalized_title(title):
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def scan_candidates(now=None, lookback_hours=None):
    now = now or datetime.now(timezone.utc)
    if lookback_hours is None:
        lookback_hours = float(os.getenv("CLIP_RADAR_LOOKBACK_HOURS", "24"))
    started=now-timedelta(hours=lookback_hours)
    token=get_app_token()
    users=get_user_ids(token, STREAMERS)
    candidates=[]
    for name in STREAMERS:
        user=users.get(name.lower())
        if not user:
            print(f"WARN: streamer not found: {name}")
            continue
        clips = get_clips(token,user["id"],started,now)
        games = get_games(token, [clip.get("game_id") for clip in clips])
        for c in clips:
            c["streamer"]=user["display_name"]
            c["broadcaster_name"] = user["display_name"]
            c["broadcaster_login"] = user.get("login", name)
            c["game_name"] = games.get(c.get("game_id"), "")
            c["score"]=total_score(c,now)
            _, age_h, views, vph=clip_stats(c,now)
            c["age_h"]=age_h; c["vph"]=vph
            # Reject extremely weak clips unless they are exploding immediately.
            if views >= 25 or vph >= 60:
                candidates.append(c)

    ranked=sorted(candidates,key=lambda x:x["score"],reverse=True)
    # Light dedupe: avoid several clips with effectively identical titles from dominating output.
    seen=set(); final=[]
    for c in ranked:
        key=(c["streamer"].lower(), normalized_title(c.get("title")))
        if key in seen: continue
        seen.add(key); final.append(c)
        if len(final) >= int(os.getenv("MAX_RANKED_CANDIDATES", "20")): break

    return final


def print_candidates(candidates, now=None):
    now = now or datetime.now(timezone.utc)
    print(f"Clip Radar scan @ {now.isoformat()} | streamers={len(STREAMERS)} | qualified={len(candidates)}")
    for i,c in enumerate(candidates,1):
        print(f"{i:02d}. score={c['score']:>6} | {c['streamer']} | views={c.get('view_count',0)} | vph={c['vph']:.1f} | age={c['age_h']:.1f}h | dur={c.get('duration',0)}s | {c.get('game_name','')} | {c.get('title')} | {c.get('url')}")


def main():
    parser = argparse.ArgumentParser(description="Scan recent Twitch clips for viral Clip Radar candidates")
    parser.add_argument("--json-out", type=Path, help="also write ranked candidates as JSON")
    parser.add_argument("--lookback-hours", type=float, default=24)
    args = parser.parse_args()
    candidates = scan_candidates(lookback_hours=args.lookback_hours)
    print_candidates(candidates)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(candidates, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
