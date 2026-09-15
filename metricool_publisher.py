"""Gated Metricool publisher for already validated Clip Radar videos.

The default mode is inert.  Dry-run creates sanitized, platform-specific
request payloads without contacting Metricool.  Live scheduling requires an
explicit enable flag and all three Metricool account identifiers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import requests

from publication_state import PublicationLedger
from publication_types import ValidatedClip
from quality_control import validate_final
from rights_gate import eligible_broadcaster
from schedule_slots import DEFAULT_TIMEZONE, next_production_slot


SUPPORTED_NETWORKS = ("tiktok", "instagram", "youtube")
LIVE_NETWORKS = ("tiktok", "instagram")
REQUIRED_CREDENTIALS = (
    "METRICOOL_USER_TOKEN",
    "METRICOOL_USER_ID",
    "METRICOOL_BLOG_ID",
)


class PublicationBlocked(RuntimeError):
    """Raised when a candidate cannot safely enter the publishing boundary."""


class MetricoolAPIError(RuntimeError):
    """Raised when Metricool rejects a request or returns an unusable payload."""


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _configured_networks() -> tuple[str, ...]:
    raw = os.getenv("METRICOOL_NETWORKS", ",".join(LIVE_NETWORKS))
    networks = tuple(dict.fromkeys(item.strip().lower() for item in raw.split(",") if item.strip()))
    unknown = sorted(set(networks) - set(SUPPORTED_NETWORKS))
    if unknown:
        raise ValueError(f"unsupported Metricool network(s): {', '.join(unknown)}")
    if not networks:
        raise PublicationBlocked("at least one Metricool network must be configured")
    if "youtube" in networks and not _env_bool("ENABLE_YOUTUBE_PUBLISHING"):
        raise PublicationBlocked("YouTube publishing is disabled until the account is recovered")
    return networks


@dataclass(frozen=True)
class MetricoolConfig:
    enabled: bool
    dry_run: bool
    networks: tuple[str, ...]
    timezone_name: str
    base_url: str
    user_token: str | None
    user_id: str | None
    blog_id: str | None
    max_daily_publications: int = 4

    @classmethod
    def from_env(cls) -> "MetricoolConfig":
        return cls(
            enabled=_env_bool("PUBLISHING_ENABLED"),
            dry_run=_env_bool("PUBLISHING_DRY_RUN"),
            networks=_configured_networks(),
            timezone_name=os.getenv("PRODUCTION_TIMEZONE", DEFAULT_TIMEZONE),
            base_url=os.getenv("METRICOOL_BASE_URL", "https://app.metricool.com/api").rstrip("/"),
            user_token=os.getenv("METRICOOL_USER_TOKEN") or None,
            user_id=os.getenv("METRICOOL_USER_ID") or None,
            blog_id=os.getenv("METRICOOL_BLOG_ID") or None,
            max_daily_publications=max(1, int(os.getenv("MAX_DAILY_PUBLICATIONS", "4"))),
        )

    def missing_credentials(self) -> list[str]:
        values = {
            "METRICOOL_USER_TOKEN": self.user_token,
            "METRICOOL_USER_ID": self.user_id,
            "METRICOOL_BLOG_ID": self.blog_id,
        }
        return [name for name in REQUIRED_CREDENTIALS if not values[name]]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "networks": list(self.networks),
            "youtube_enabled": "youtube" in self.networks,
            "max_daily_publications": self.max_daily_publications,
            "timezone": self.timezone_name,
            "base_url": self.base_url,
            "credentials_configured": not self.missing_credentials(),
            "credential_names": list(REQUIRED_CREDENTIALS),
        }


def _candidate_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "clip_id": str(candidate.get("id") or candidate.get("clip_id") or ""),
        "broadcaster": str(candidate.get("streamer") or candidate.get("broadcaster_name") or ""),
        "title": str(candidate.get("title") or ""),
        "game": str(candidate.get("game_name") or ""),
        "source_url": str(candidate.get("url") or ""),
        "viral_score": candidate.get("score"),
        "views": candidate.get("view_count") or candidate.get("views"),
        "views_per_hour": candidate.get("vph"),
        "duration": candidate.get("duration"),
    }


class MetricoolPublisher:
    def __init__(self, config: MetricoolConfig | None = None, session: requests.Session | None = None):
        self.config = config or MetricoolConfig.from_env()
        self.session = session or requests.Session()
        self._last_planned_slot: datetime | None = None

    def validate_input(self, clip: ValidatedClip) -> None:
        if not clip.candidate.get("id") and not clip.candidate.get("clip_id"):
            raise PublicationBlocked("publishing requires an immutable Twitch clip ID")
        if eligible_broadcaster(dict(clip.candidate)) is None:
            raise PublicationBlocked("publishing requires a verified sharing whitelist entry")
        if clip.qc_status != "ready_for_publish_queue":
            raise PublicationBlocked("publishing requires QC status ready_for_publish_queue")
        ok, reason = validate_final(clip.final_path)
        if not ok:
            raise PublicationBlocked(f"publishing input failed final QC: {reason}")

    def build_plan(
        self,
        clip: ValidatedClip,
        metadata: Mapping[str, Any],
        slot: datetime | None = None,
    ) -> dict[str, Any]:
        self.validate_input(clip)
        if slot is None:
            slot = next_production_slot(
                now=self._last_planned_slot,
                timezone_name=self.config.timezone_name,
            )
        self._last_planned_slot = slot
        fields = _candidate_fields(clip.candidate)
        requests_by_network: dict[str, dict[str, Any]] = {}
        for network in self.config.networks:
            network_metadata = dict(metadata.get(network) or {})
            body: dict[str, Any] = {
                "publicationDate": {
                    "dateTime": slot.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timezone": self.config.timezone_name,
                },
                "text": network_metadata.get("caption") or network_metadata.get("description", ""),
                "providers": [{"network": network}],
                "media": ["<METRICOOL_MEDIA_URL_AFTER_UPLOAD>"],
                "autoPublish": True,
                "draft": False,
                "saveExternalMediaFiles": True,
                "shortener": False,
            }
            if network == "instagram":
                body["instagramData"] = {"type": "REEL", "autoPublish": True, "showReelOnFeed": True}
            elif network == "tiktok":
                body["tiktokData"] = {"title": network_metadata.get("title") or fields["title"]}
            requests_by_network[network] = {
                "endpoint": "/v2/scheduler/posts",
                "body": body,
                "metadata": network_metadata,
            }

        return {
            "schema_version": 1,
            "status": "DRY_RUN_READY" if self.config.dry_run else "QUEUED",
            "candidate": fields,
            "rights": {
                "status": "verified",
                "reason": "viewer_social_sharing=true in sharing_whitelist.json",
                "verified_broadcaster": eligible_broadcaster(dict(clip.candidate)),
            },
            "qc": {"status": clip.qc_status, "final_output": str(clip.final_path)},
            "publication_slot": {
                "timezone": self.config.timezone_name,
                "date_time": slot.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "next_available_production_slot",
            },
            "networks": requests_by_network,
            "disabled_networks": ["youtube"] if "youtube" not in self.config.networks else [],
            "all_generated_metadata": dict(metadata),
            "media_upload": {
                "required": True,
                "method": "Metricool S3 direct upload before scheduler request",
                "content_type": "video/mp4",
                "source_file": str(clip.final_path),
            },
            "configuration": self.config.safe_summary(),
            "live_request_sent": False,
        }

    def publish(
        self,
        clip: ValidatedClip,
        metadata: Mapping[str, Any],
        ledger: PublicationLedger,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload and schedule one validated clip, one network at a time."""

        if not self.config.enabled:
            raise PublicationBlocked("PUBLISHING_ENABLED is false")
        if self.config.dry_run:
            raise PublicationBlocked("dry-run mode cannot send live Metricool requests")
        missing = self.config.missing_credentials()
        if missing:
            raise PublicationBlocked("missing Metricool configuration: " + ", ".join(missing))
        plan = plan or self.build_plan(clip, metadata)
        clip_id = plan["candidate"]["clip_id"]
        if ledger.is_published(clip_id) or ledger.is_active(clip_id):
            raise PublicationBlocked("clip already has active or successful publication state")
        local_date = str(plan["publication_slot"]["date_time"])[:10]
        if ledger.count_scheduled_on_date(local_date) >= self.config.max_daily_publications:
            raise PublicationBlocked(
                f"daily publication cap reached for {local_date} ({self.config.max_daily_publications})"
            )
        ledger.upsert_clip(
            clip_id,
            "PUBLISHING",
            broadcaster=plan["candidate"]["broadcaster"],
            source_url=plan["candidate"]["source_url"],
            final_output_identifier=str(clip.final_path),
            publication_timestamp=None,
        )
        try:
            media_url = self._upload_video(clip.final_path)
        except Exception as exc:
            ledger.upsert_clip(clip_id, "FAILED", error=self._safe_error(exc))
            raise
        results: dict[str, Any] = {}
        for network, request_plan in plan["networks"].items():
            job_id = f"clip-radar-{clip_id}-{network}"[:120]
            body = json.loads(json.dumps(request_plan["body"]).replace("<METRICOOL_MEDIA_URL_AFTER_UPLOAD>", media_url))
            try:
                response = self._schedule_post(body, job_id)
                metricool_id = self._extract_post_id(response)
                result = {
                    "status": "QUEUED",
                    "metricool_id": metricool_id,
                    "response_status": "accepted",
                }
                ledger.update_network(clip_id, network, "QUEUED", **result)
            except Exception as exc:
                result = {"status": "FAILED", "error": self._safe_error(exc)}
                ledger.update_network(clip_id, network, "FAILED", **result)
            results[network] = result
        final_status = "QUEUED" if results and all(item["status"] == "QUEUED" for item in results.values()) else "FAILED"
        ledger.upsert_clip(clip_id, final_status, metricool_media_stored=True)
        return {"status": final_status, "clip_id": clip_id, "media_url_stored": True, "networks": results}

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Keep request URLs, tokens, and presigned media locations out of artifacts."""

        if isinstance(exc, (MetricoolAPIError, PublicationBlocked)):
            return str(exc)
        return f"{type(exc).__name__}: Metricool request failed"

    def _auth_headers(self) -> dict[str, str]:
        if not self.config.user_token:
            raise PublicationBlocked("Metricool token is unavailable")
        return {"X-Mc-Auth": self.config.user_token, "Content-Type": "application/json"}

    def _api_url(self, path: str) -> str:
        return self.config.base_url + path

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        params = dict(kwargs.pop("params", {}))
        params.update({"blogId": self.config.blog_id, "userId": self.config.user_id})
        response = self.session.request(
            method,
            self._api_url(path),
            params=params,
            headers=self._auth_headers(),
            timeout=120,
            **kwargs,
        )
        if not response.ok:
            raise MetricoolAPIError(f"Metricool {method} {path} failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetricoolAPIError(f"Metricool {method} {path} returned non-JSON") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def _upload_video(self, path: Path) -> str:
        size = path.stat().st_size
        chunk_size = 5 * 1024 * 1024
        parts: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            start = 0
            while start < size:
                data = handle.read(min(chunk_size, size - start))
                end = start + len(data)
                digest = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
                parts.append({"size": len(data), "startByte": start, "endByte": end, "hash": digest})
                start = end
        created = self._request_json(
            "PUT",
            "/v2/media/s3/upload-transactions",
            json={
                "resourceType": "planner",
                "contentType": "video/mp4",
                "fileExtension": "mp4",
                "parts": parts,
            },
        )
        transaction = created.get("data") or created
        upload_type = str(transaction.get("uploadType") or "").upper()
        if upload_type == "SIMPLE":
            presigned = transaction.get("presignedUrl")
            if not presigned:
                raise MetricoolAPIError("Metricool did not return a simple upload URL")
            with path.open("rb") as handle:
                response = self.session.put(
                    presigned,
                    data=handle,
                    headers={"Content-Type": "video/mp4"},
                    timeout=180,
                )
            if not response.ok:
                raise MetricoolAPIError(f"Metricool media upload failed with HTTP {response.status_code}")
            completed = self._request_json(
                "PATCH",
                "/v2/media/s3/upload-transactions",
                json={"simple": {"fileUrl": transaction.get("fileUrl")}},
            )
            return self._extract_media_url(completed or transaction)

        upload_id = transaction.get("uploadId")
        bucket = transaction.get("bucket")
        key = transaction.get("key")
        presigned_parts = transaction.get("parts") or []
        if not upload_id or not bucket or not key or len(presigned_parts) != len(parts):
            raise MetricoolAPIError("Metricool returned an incomplete multipart upload transaction")
        uploaded: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            for index, item in enumerate(presigned_parts):
                data = handle.read(parts[index]["size"])
                response = self.session.put(
                    item["presignedUrl"],
                    data=data,
                    headers={"Content-Type": "video/mp4"},
                    timeout=180,
                )
                if not response.ok:
                    raise MetricoolAPIError(f"Metricool media part upload failed with HTTP {response.status_code}")
                etag = response.headers.get("ETag") or response.headers.get("etag")
                if not etag:
                    raise MetricoolAPIError("Metricool media part upload returned no ETag")
                uploaded.append({"partNumber": index + 1, "etag": etag})
        completed = self._request_json(
            "PATCH",
            "/v2/media/s3/upload-transactions",
            json={"multipart": {"uploadId": upload_id, "key": key, "parts": uploaded}},
        )
        return self._extract_media_url(completed)

    def _schedule_post(self, body: dict[str, Any], job_id: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._request_json("POST", "/v2/scheduler/posts", params={"jobId": job_id}, json=body)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise MetricoolAPIError("Metricool request outcome was ambiguous after bounded retry") from exc
            except MetricoolAPIError:
                raise
        raise MetricoolAPIError("Metricool request failed") from last_error

    @staticmethod
    def _extract_media_url(payload: Mapping[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        url = data.get("convertedFileUrl") or data.get("fileUrl") or data.get("url")
        if not url:
            raise MetricoolAPIError("Metricool upload response did not contain a media URL")
        return str(url)

    @staticmethod
    def _extract_post_id(payload: Mapping[str, Any]) -> Any:
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        return data.get("id") or data.get("uuid") or data.get("postId")
