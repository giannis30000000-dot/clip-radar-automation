"""Read-only audit of recent Buffer posts for controlled-run verification."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


class BufferAuditError(RuntimeError):
    """Raised when the read-only Buffer audit cannot complete safely."""


def _safe_error(value: Any, api_key: str) -> str:
    message = str(value or "Buffer audit failed")
    if api_key:
        message = message.replace(api_key, "<redacted>")
    return message[:500]


def _request(api_key: str, query: str) -> dict[str, Any]:
    try:
        response = requests.post(
            "https://api.buffer.com",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise BufferAuditError(_safe_error(exc, api_key)) from exc
    if not isinstance(payload, dict):
        raise BufferAuditError("Buffer audit returned an invalid JSON object")
    if payload.get("errors"):
        messages = "; ".join(str(item.get("message") or "GraphQL error") for item in payload["errors"])
        raise BufferAuditError(_safe_error(messages, api_key))
    return payload


def _service(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _recent(value: Any, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone.utc) >= cutoff


def audit(output_path: Path | None = None, lookback_minutes: int | None = None) -> dict[str, Any]:
    api_key = (os.getenv("BUFFER_API_KEY") or "").strip()
    if not api_key:
        raise BufferAuditError("BUFFER_API_KEY is required for a read-only Buffer audit")
    output_path = output_path or Path(os.getenv("CLIP_RADAR_OUTPUT_DIR", "output")) / "buffer_audit.json"
    lookback_minutes = lookback_minutes or int(os.getenv("BUFFER_AUDIT_LOOKBACK_MINUTES", "180"))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, lookback_minutes))

    organizations_payload = _request(
        api_key,
        """
        query GetOrganizations {
          account { organizations { id name } }
        }
        """,
    )
    organizations = organizations_payload.get("data", {}).get("account", {}).get("organizations") or []
    if not organizations:
        raise BufferAuditError("Buffer account returned no organizations")

    channels: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []
    for organization in organizations:
        organization_id = str(organization.get("id") or "")
        if not organization_id:
            continue
        channels_payload = _request(
            api_key,
            f"""
            query GetChannels {{
              channels(input: {{ organizationId: {json.dumps(organization_id)} }}) {{
                id name service
              }}
            }}
            """,
        )
        organization_channels = channels_payload.get("data", {}).get("channels") or []
        public_channels = [
            {
                "id": str(channel.get("id") or ""),
                "name": str(channel.get("name") or ""),
                "service": _service(channel.get("service")),
                "organization_id": organization_id,
                "organization_name": str(organization.get("name") or ""),
            }
            for channel in organization_channels
        ]
        channels.extend(public_channels)
        relevant = [
            channel
            for channel in public_channels
            if channel["service"] in {"instagram", "tiktok"}
            and channel["name"].strip().lower() in {"clipradar01", "clipradar001"}
        ]
        if not relevant:
            continue
        channel_ids = ", ".join(json.dumps(channel["id"]) for channel in relevant)
        after: str | None = None
        while True:
            after_clause = f"after: {json.dumps(after)}," if after else ""
            payload = _request(
                api_key,
                f"""
                query GetPosts {{
                  posts(
                    {after_clause}
                    first: 100,
                    input: {{ organizationId: {json.dumps(organization_id)}, filter: {{ channelIds: [{channel_ids}] }} }}
                  ) {{
                    pageInfo {{ hasNextPage endCursor }}
                    edges {{ node {{ id text createdAt dueAt channelId status }} }}
                  }}
                }}
                """,
            )
            page = payload.get("data", {}).get("posts") or {}
            for edge in page.get("edges") or []:
                node = edge.get("node") or {}
                if not _recent(node.get("createdAt"), cutoff):
                    continue
                posts.append(
                    {
                        "id": str(node.get("id") or ""),
                        "channel_id": str(node.get("channelId") or ""),
                        "status": str(node.get("status") or ""),
                        "created_at": node.get("createdAt"),
                        "due_at": node.get("dueAt"),
                        "text": str(node.get("text") or "")[:240],
                    }
                )
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            after = str(page_info["endCursor"])

    channel_by_id = {channel["id"]: channel for channel in channels}
    for post in posts:
        channel = channel_by_id.get(post["channel_id"], {})
        post["channel_name"] = channel.get("name")
        post["service"] = channel.get("service")
    result = {
        "schema_version": 1,
        "status": "READY",
        "lookback_minutes": lookback_minutes,
        "cutoff": cutoff.isoformat(),
        "organizations": [
            {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
            for item in organizations
        ],
        "channels": channels,
        "posts": posts,
        "post_count": len(posts),
        "credential_names": ["BUFFER_API_KEY"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    try:
        result = audit()
    except BufferAuditError as exc:
        print(f"buffer_audit | BLOCKED | reason={exc}")
        raise SystemExit(2) from exc
    print(f"buffer_audit | READY | posts={result['post_count']}")
