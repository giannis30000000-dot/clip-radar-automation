import os
import math
import re
import requests
from datetime import datetime, timedelta, timezone

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
    r = requests.get("https://api.twitch.tv/helix/clips", headers=headers(token), params={"broadcaster_id": broadcaster_id,"started_at": started_at.isoformat().replace("+00:00", "Z"),"ended_at": ended_at.isoformat().replace("+00:00", "Z"),"first": 100}, timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


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
    hook_words=["wtf","crazy","no way","delivery","rage","funny","reaction","insane","caught","reveals","gets","irl","fight","shocked","package"]
    hook=min(100, sum(18 for k in hook_words if k in title))
    # Penalize near-zero traction heavily so brand-new 4-view clips do not outrank proven moments.
    low_penalty = 35 if views < 20 else (18 if views < 75 else 0)
    return round(0.24*freshness + 0.31*velocity + 0.25*traction + 0.12*duration_score + 0.08*hook - low_penalty, 2)


def normalized_title(title):
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def main():
    now=datetime.now(timezone.utc)
    started=now-timedelta(hours=24)
    token=get_app_token()
    users=get_user_ids(token, STREAMERS)
    candidates=[]
    for name in STREAMERS:
        user=users.get(name.lower())
        if not user:
            print(f"WARN: streamer not found: {name}")
            continue
        for c in get_clips(token,user["id"],started,now):
            c["streamer"]=user["display_name"]
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
        if len(final)>=20: break

    print(f"Clip Radar scan @ {now.isoformat()} | streamers={len(STREAMERS)} | qualified={len(candidates)}")
    for i,c in enumerate(final,1):
        print(f"{i:02d}. score={c['score']:>6} | {c['streamer']} | views={c.get('view_count',0)} | vph={c['vph']:.1f} | age={c['age_h']:.1f}h | dur={c.get('duration',0)}s | {c.get('title')} | {c.get('url')}")

if __name__ == "__main__":
    main()
