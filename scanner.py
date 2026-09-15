import os
import math
import requests
from datetime import datetime, timedelta, timezone

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
STREAMERS = [s.strip() for s in os.getenv("STREAMERS", "xqc,kaicenat,jynxzi,caseoh_" ).split(",") if s.strip()]


def get_app_token():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET")
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token):
    return {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }


def get_user_ids(token, names):
    params = []
    for n in names:
        params.append(("login", n))
    r = requests.get("https://api.twitch.tv/helix/users", headers=headers(token), params=params, timeout=20)
    r.raise_for_status()
    return {u["login"].lower(): u for u in r.json().get("data", [])}


def get_clips(token, broadcaster_id, started_at, ended_at):
    params = {
        "broadcaster_id": broadcaster_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
        "first": 100,
    }
    r = requests.get("https://api.twitch.tv/helix/clips", headers=headers(token), params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


def freshness_score(created_at, now):
    age_h = max(0.01, (now - created_at).total_seconds() / 3600)
    return max(0, 100 - age_h * 4)


def velocity_score(views, created_at, now):
    age_h = max(0.5, (now - created_at).total_seconds() / 3600)
    vph = views / age_h
    return min(100, 18 * math.log10(vph + 1))


def total_score(clip, now):
    created = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    views = int(clip.get("view_count", 0))
    fresh = freshness_score(created, now)
    velocity = velocity_score(views, created, now)
    duration = float(clip.get("duration", 0))
    duration_bonus = 10 if 8 <= duration <= 60 else 0
    title = (clip.get("title") or "").lower()
    hook_bonus = 0
    for k in ["wtf", "crazy", "no way", "delivery", "rage", "funny", "reaction", "insane", "caught"]:
        if k in title:
            hook_bonus += 3
    return round(0.45 * fresh + 0.45 * velocity + duration_bonus + min(10, hook_bonus), 2)


def main():
    now = datetime.now(timezone.utc)
    started = now - timedelta(hours=24)
    token = get_app_token()
    users = get_user_ids(token, STREAMERS)
    all_clips = []
    for name in STREAMERS:
        user = users.get(name.lower())
        if not user:
            print(f"WARN: streamer not found: {name}")
            continue
        clips = get_clips(token, user["id"], started, now)
        for c in clips:
            c["streamer"] = user["display_name"]
            c["score"] = total_score(c, now)
            all_clips.append(c)

    ranked = sorted(all_clips, key=lambda x: x["score"], reverse=True)[:20]
    print(f"Clip Radar scan @ {now.isoformat()} | streamers={len(STREAMERS)} | clips={len(all_clips)}")
    for i, c in enumerate(ranked, 1):
        print(
            f"{i:02d}. score={c['score']:>6} | {c['streamer']} | views={c.get('view_count',0)} | "
            f"dur={c.get('duration',0)}s | {c.get('created_at')} | {c.get('title')} | {c.get('url')}"
        )


if __name__ == "__main__":
    main()
