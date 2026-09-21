"""Restore/save durable story history on a dedicated Git branch.

Actions caches are evictable, so they are not the premise source of truth.
Only this known history file is written. Compare-and-swap rejects stale writers.
The workflow serializes runs and provides its short-lived GITHUB_TOKEN.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys

import requests

from .history import StoryHistory, write_json

BRANCH = "clip-radar-story-state"
REMOTE_PATH = ".clip-radar/story_history.json"
LOCAL = Path("state/story_history.json")
REMOTE = Path("state/story_remote.json")


def sync(action: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    base = f"https://api.github.com/repos/{repo}"

    def call(method, path, *, allowed=(200,), **kwargs):
        response = session.request(method, base + path, timeout=45, **kwargs)
        if response.status_code not in allowed:
            # Do not include request headers, tokens or provider response bodies.
            raise RuntimeError(f"Story history {method} failed: HTTP {response.status_code}")
        return response

    if action == "restore":
        ref = call("GET", f"/git/ref/heads/{BRANCH}", allowed=(200, 404))
        if ref.status_code == 404:
            write_json(REMOTE, {"sha": None, "branch_missing": True})
            write_json(LOCAL, {"version": 1, "stories": {}})
        else:
            response = call("GET", f"/contents/{REMOTE_PATH}", params={"ref": BRANCH}).json()
            data = json.loads(base64.b64decode(response["content"]))
            write_json(LOCAL, data)
            StoryHistory(LOCAL).read()  # refuse corrupt history instead of resetting
            write_json(REMOTE, {"sha": response["sha"], "branch_missing": False})
        print("story history | restored")
    elif action == "save":
        if not LOCAL.exists() or not REMOTE.exists():
            print("story history | no restored state to save")
            return
        data = StoryHistory(LOCAL).read()
        previous = json.loads(REMOTE.read_text(encoding="utf-8"))
        if previous["branch_missing"]:
            call("POST", "/git/refs", allowed=(201,), json={"ref": f"refs/heads/{BRANCH}", "sha": os.environ["GITHUB_SHA"]})
        payload = {
            "message": "Record original story generation history",
            "branch": BRANCH,
            "content": base64.b64encode((json.dumps(data, indent=2) + "\n").encode()).decode(),
        }
        if previous["sha"]:
            payload["sha"] = previous["sha"]
        call("PUT", f"/contents/{REMOTE_PATH}", allowed=(200, 201), json=payload)
        print(f"story history | saved {len(data['stories'])} premises")
    else:
        raise ValueError("action must be restore or save")


if __name__ == "__main__":
    sync(sys.argv[1])
