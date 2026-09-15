"""Gated Buffer GraphQL publisher for Clip Radar.

Buffer is the active backend.  Discovery is read-only and happens before any
post mutation.  Dry-run mode calls only account/channel queries and writes a
sanitized request plan; live mode requires an explicit enable flag, verified
channel selection, and a stable public media URL because Buffer fetches video
from the URL supplied in the post asset.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests
from urllib.parse import urlparse

from publication_state import PublicationLedger
from publication_types import ValidatedClip
from quality_control import validate_final
from rights_gate import ACCEPTED_RIGHTS_BASES, eligible_broadcaster, rights_evidence
from schedule_slots import DEFAULT_TIMEZONE, next_production_slot


SUPPORTED_NETWORKS = ("tiktok", "instagram", "youtube")
LIVE_NETWORKS = ("tiktok", "instagram")
REQUIRED_CREDENTIALS = ("BUFFER_API_KEY",)


class PublicationBlocked(RuntimeError):
    """Raised when a post cannot safely cross the publication boundary."""


class BufferAPIError(RuntimeError):
    """Raised for an HTTP or GraphQL Buffer error."""


@dataclass(frozen=True)
class BufferConfig:
    enabled: bool
    dry_run: bool
    networks: tuple[str, ...]
    timezone_name: str
    api_url: str
    api_key: str | None
    channel_config_path: Path
    brand_name: str
    instagram_channel_id: str | None
    tiktok_channel_id: str | None
    youtube_channel_id: str | None
    media_url: str | None
    max_daily_publications: int = 4

    @classmethod
    def from_env(cls) -> "BufferConfig":
        raw = os.getenv("BUFFER_NETWORKS", ",".join(LIVE_NETWORKS))
        networks = tuple(dict.fromkeys(item.strip().lower() for item in raw.split(",") if item.strip()))
        unknown = sorted(set(networks) - set(SUPPORTED_NETWORKS))
        if unknown:
            raise ValueError(f"unsupported Buffer network(s): {', '.join(unknown)}")
        if not networks:
            raise PublicationBlocked("at least one Buffer network must be configured")
        if "youtube" in networks and not _env_bool("ENABLE_YOUTUBE_PUBLISHING"):
            raise PublicationBlocked("YouTube publishing is disabled until the account is recovered")
        return cls(
            enabled=_env_bool("PUBLISHING_ENABLED"),
            dry_run=_env_bool("PUBLISHING_DRY_RUN"),
            networks=networks,
            timezone_name=os.getenv("PRODUCTION_TIMEZONE", DEFAULT_TIMEZONE),
            api_url=os.getenv("BUFFER_API_URL", "https://api.buffer.com").rstrip("/"),
            api_key=os.getenv("BUFFER_API_KEY") or None,
            channel_config_path=Path(os.getenv("BUFFER_CHANNEL_CONFIG", "state/buffer_channels.json")),
            brand_name=os.getenv("BUFFER_BRAND_NAME", "Clip Radar").strip(),
            instagram_channel_id=os.getenv("BUFFER_INSTAGRAM_CHANNEL_ID") or None,
            tiktok_channel_id=os.getenv("BUFFER_TIKTOK_CHANNEL_ID") or None,
            youtube_channel_id=os.getenv("BUFFER_YOUTUBE_CHANNEL_ID") or None,
            media_url=os.getenv("BUFFER_MEDIA_URL") or None,
            max_daily_publications=max(1, int(os.getenv("MAX_DAILY_PUBLICATIONS", "4"))),
        )

    def missing_credentials(self) -> list[str]:
        return ["BUFFER_API_KEY"] if not self.api_key else []

    def safe_summary(self) -> dict[str, Any]:
        return {
            "backend": "buffer",
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "networks": list(self.networks),
            "youtube_enabled": "youtube" in self.networks,
            "timezone": self.timezone_name,
            "api_url": self.api_url,
            "brand_name": self.brand_name,
            "channel_config_path": str(self.channel_config_path),
            "credentials_configured": not self.missing_credentials(),
            "credential_names": list(REQUIRED_CREDENTIALS),
            "media_url_configured": bool(self.media_url),
            "max_daily_publications": self.max_daily_publications,
        }


@dataclass(frozen=True)
class BufferDiscovery:
    organizations: tuple[dict[str, Any], ...]
    channels: tuple[dict[str, Any], ...]
    selected: Mapping[str, dict[str, Any]]
    selection_basis: str
    api_calls: int
    rate_limits: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "organizations": list(self.organizations),
            "channels": list(self.channels),
            "selected": dict(self.selected),
            "selection_basis": self.selection_basis,
            "api_calls": self.api_calls,
            "rate_limits": list(self.rate_limits),
        }


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _scheduled_override(value: str | None, timezone_name: str) -> datetime | None:
    """Parse an explicit UTC/offset schedule used only by controlled live tests."""

    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationBlocked("BUFFER_SCHEDULE_AT must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError) as exc:
        raise PublicationBlocked(f"invalid production timezone: {timezone_name}") from exc


def _safe_rate_limits(response: requests.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if "rate" in key.lower() or key.lower() in {"retry-after"}
    }


def _channel_service(channel: Mapping[str, Any]) -> str:
    return str(channel.get("service") or "").strip().lower().replace("-", "_")


def _channel_public(channel: Mapping[str, Any], organization: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(channel.get("id") or ""),
        "name": str(channel.get("name") or ""),
        "service": _channel_service(channel),
        "organization_id": str(organization.get("id") or ""),
        "organization_name": str(organization.get("name") or ""),
    }


class BufferPublisher:
    def __init__(self, config: BufferConfig | None = None, session: requests.Session | None = None):
        self.config = config or BufferConfig.from_env()
        self.session = session or requests.Session()
        self._last_planned_slot: datetime | None = None
        self._discovery: BufferDiscovery | None = None
        self._api_calls = 0
        self._rate_limits: list[dict[str, str]] = []

    def validate_input(self, clip: ValidatedClip) -> None:
        if not clip.candidate.get("id") and not clip.candidate.get("clip_id"):
            raise PublicationBlocked("publishing requires an immutable Twitch clip ID")
        if eligible_broadcaster(dict(clip.candidate)) is None:
            raise PublicationBlocked("publishing requires a verified sharing whitelist entry")
        if clip.qc_status != "ready_for_publish_queue":
            raise PublicationBlocked("publishing requires QC status ready_for_publish_queue")
        if clip.rights_basis not in ACCEPTED_RIGHTS_BASES:
            raise PublicationBlocked("publishing requires an accepted documented rights basis")
        if clip.third_party_check != "THIRD_PARTY_CHECK_PASSED":
            raise PublicationBlocked("publishing requires the third-party content check to pass")
        if clip.transformation_check != "TRANSFORMATION_CHECK_PASSED":
            raise PublicationBlocked("publishing requires the transformation check to pass")
        ok, reason = validate_final(clip.final_path)
        if not ok:
            raise PublicationBlocked(f"publishing input failed final QC: {reason}")

    def discover_channels(self, force: bool = False) -> BufferDiscovery:
        if self._discovery is not None and not force:
            return self._discovery
        if not force and self.config.channel_config_path.exists():
            try:
                cached = json.loads(self.config.channel_config_path.read_text(encoding="utf-8"))
                selected = cached.get("selected") or {}
                if all(network in selected for network in self.config.networks if network != "youtube"):
                    self._discovery = BufferDiscovery(
                        organizations=tuple(cached.get("organizations") or []),
                        channels=tuple(cached.get("channels") or []),
                        selected=selected,
                        selection_basis=str(cached.get("selection_basis") or "cached_verified_discovery"),
                        api_calls=0,
                        rate_limits=tuple(),
                    )
                    return self._discovery
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        if not self.config.api_key:
            raise PublicationBlocked("BUFFER_API_KEY is required for Buffer channel discovery")

        organizations_payload = self._graphql(
            """
            query GetOrganizations {
              account {
                organizations { id name }
              }
            }
            """
        )
        organizations = tuple(organizations_payload.get("data", {}).get("account", {}).get("organizations") or [])
        if not organizations:
            raise PublicationBlocked("Buffer account returned no organizations")
        channels: list[dict[str, Any]] = []
        for organization in organizations:
            organization_id = str(organization.get("id") or "")
            if not organization_id:
                continue
            query = f"""
            query GetChannels {{
              channels(input: {{ organizationId: {json.dumps(organization_id)} }}) {{
                id name service
              }}
            }}
            """
            payload = self._graphql(query)
            raw_channels = payload.get("data", {}).get("channels") or []
            channels.extend(_channel_public(channel, organization) for channel in raw_channels)
        selected = self._select_channels(channels)
        discovery = BufferDiscovery(
            organizations=tuple({"id": str(item.get("id") or ""), "name": str(item.get("name") or "")} for item in organizations),
            channels=tuple(channels),
            selected=selected,
            selection_basis=self._selection_basis(channels, selected),
            api_calls=self._api_calls,
            rate_limits=tuple(self._rate_limits),
        )
        self.config.channel_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.channel_config_path.write_text(json.dumps(discovery.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._discovery = discovery
        return discovery

    def build_plan(
        self,
        clip: ValidatedClip,
        metadata: Mapping[str, Any],
        slot: datetime | None = None,
        media_url: str | None = None,
        media_delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.validate_input(clip)
        discovery = self.discover_channels(force=_env_bool("BUFFER_REFRESH_CHANNELS"))
        if slot is None:
            slot = _scheduled_override(
                os.getenv("BUFFER_SCHEDULE_AT"), self.config.timezone_name
            )
            schedule_source = "controlled_schedule_override" if slot else "next_available_production_slot"
            if slot is None:
                slot = next_production_slot(
                    now=self._last_planned_slot, timezone_name=self.config.timezone_name
                )
        else:
            schedule_source = "explicit_slot"
        self._last_planned_slot = slot
        fields = _candidate_fields(clip.candidate)
        media_url = media_url or self.config.media_url or "<PUBLIC_BUFFER_MEDIA_URL_REQUIRED>"
        requests_by_network: dict[str, dict[str, Any]] = {}
        due_at = slot.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for network in self.config.networks:
            channel = discovery.selected[network]
            network_metadata = dict(metadata.get(network) or {})
            input_data: dict[str, Any] = {
                "text": network_metadata.get("caption") or network_metadata.get("description") or "",
                "channelId": channel["id"],
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due_at,
                "assets": [{"video": {"url": media_url, "metadata": {"thumbnailOffset": 2000}}}],
                "source": "clip-radar",
            }
            if network == "instagram":
                input_data["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
            elif network == "tiktok":
                input_data["metadata"] = {"tiktok": {"title": network_metadata.get("title") or fields["title"]}}
            elif network == "youtube":
                input_data["metadata"] = {
                    "youtube": {
                        "title": network_metadata.get("title") or fields["title"],
                        "categoryId": "20",
                        "privacy": "public",
                        "notifySubscribers": False,
                        "madeForKids": False,
                    }
                }
            requests_by_network[network] = {
                "mutation": "createPost",
                "channel": channel,
                "input": input_data,
                "metadata": network_metadata,
            }
        rights = rights_evidence(dict(clip.candidate))
        return {
            "schema_version": 1,
            "backend": "buffer",
            "status": "DRY_RUN_READY" if self.config.dry_run else "QUEUED",
            "candidate": fields,
            "rights": rights,
            "third_party_check": {"status": clip.third_party_check, "reason": "conservative_content_screen_passed"},
            "transformation_check": {"status": clip.transformation_check, "reason": "editorialized_render_profile_passed"},
            "publish_eligibility": "PUBLISH_ELIGIBLE",
            "qc": {"status": clip.qc_status, "final_output": str(clip.final_path)},
            "publication_slot": {
                "timezone": self.config.timezone_name,
                "local_date_time": slot.strftime("%Y-%m-%dT%H:%M:%S"),
                "utc_date_time": due_at,
                "source": schedule_source,
            },
            "channel_discovery": discovery.as_dict(),
            "channels": {network: discovery.selected[network] for network in discovery.selected if network in self.config.networks},
            "networks": requests_by_network,
            "disabled_networks": ["youtube"] if "youtube" not in self.config.networks else [],
            "all_generated_metadata": dict(metadata),
            "media": {
                **_safe_media_delivery(media_delivery),
                "required": True,
                "public_url": media_url,
                "source_file": str(clip.final_path),
                "stable_public_url_required_for_live": True,
            },
            "configuration": self.config.safe_summary(),
            "live_request_sent": False,
            "api_calls": self._api_calls,
            "rate_limits": list(self._rate_limits),
        }

    def publish(
        self,
        clip: ValidatedClip,
        metadata: Mapping[str, Any],
        ledger: PublicationLedger,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise PublicationBlocked("PUBLISHING_ENABLED is false")
        if self.config.dry_run:
            raise PublicationBlocked("dry-run mode cannot send live Buffer requests")
        if self.config.missing_credentials():
            raise PublicationBlocked("missing Buffer configuration: BUFFER_API_KEY")
        plan = plan or self.build_plan(clip, metadata)
        media_url = str((plan.get("media") or {}).get("public_url") or self.config.media_url or "")
        if not _is_public_media_url(media_url):
            raise PublicationBlocked("live Buffer publishing requires a stable public HTTPS MP4 URL")
        clip_id = plan["candidate"]["clip_id"]
        pending = {
            network: item
            for network, item in plan["networks"].items()
            if not ((ledger.get(clip_id) or {}).get("networks") or {}).get(network, {}).get("status") in {"QUEUED", "PUBLISHING", "PUBLISHED"}
        }
        if not pending:
            raise PublicationBlocked("all requested Buffer networks already have active or successful state")
        local_date = str(plan["publication_slot"]["local_date_time"])[:10]
        if ledger.count_scheduled_on_date(local_date) >= self.config.max_daily_publications:
            raise PublicationBlocked(f"daily publication cap reached for {local_date} ({self.config.max_daily_publications})")
        ledger.upsert_clip(
            clip_id,
            "PUBLISHING",
            broadcaster=plan["candidate"]["broadcaster"],
            source_url=plan["candidate"]["source_url"],
            final_output_identifier=str(clip.final_path),
            publication_slot=plan["publication_slot"],
            publication_timestamp=None,
        )
        results: dict[str, Any] = {}
        for network, request_plan in pending.items():
            try:
                payload = self._create_post(request_plan["input"])
                post = payload.get("post") or {}
                result = {
                    "status": self._map_post_status(post.get("status")),
                    "buffer_post_id": post.get("id"),
                    "channel_id": request_plan["channel"]["id"],
                    "due_at": post.get("dueAt") or plan["publication_slot"]["utc_date_time"],
                }
                ledger.update_network(clip_id, network, result["status"], **result)
            except Exception as exc:
                result = {"status": "FAILED", "error": self._safe_error(exc), "channel_id": request_plan["channel"]["id"]}
                ledger.update_network(clip_id, network, "FAILED", **result)
            results[network] = result
        final_status = "QUEUED" if results and all(item["status"] in {"QUEUED", "PUBLISHED"} for item in results.values()) else "FAILED"
        ledger.upsert_clip(clip_id, final_status, buffer_media_url_stored=True)
        return {"status": final_status, "clip_id": clip_id, "networks": results, "live_request_sent": bool(results), "api_calls": self._api_calls, "rate_limits": list(self._rate_limits)}

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise PublicationBlocked("BUFFER_API_KEY is unavailable")
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.session.post(
                    self.config.api_url,
                    headers=self._headers(),
                    json={"query": query, "variables": dict(variables or {})},
                    timeout=60,
                )
                self._api_calls += 1
                rate_limits = _safe_rate_limits(response)
                if rate_limits:
                    self._rate_limits.append(rate_limits)
                if not response.ok:
                    raise BufferAPIError(f"Buffer GraphQL request failed with HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise BufferAPIError("Buffer GraphQL request returned an invalid JSON object")
                if payload.get("errors"):
                    messages = "; ".join(str(item.get("message") or "GraphQL error") for item in payload["errors"])
                    raise BufferAPIError(f"Buffer GraphQL error: {messages[:500]}")
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise BufferAPIError("Buffer request outcome was ambiguous after bounded retry") from exc
        raise BufferAPIError("Buffer request failed") from last_error

    def _create_post(self, input_data: Mapping[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess {
              post { id channelId dueAt status }
            }
            ... on MutationError { message }
          }
        }
        """
        payload = self._graphql(mutation, {"input": input_data})
        result = payload.get("data", {}).get("createPost") or {}
        if result.get("message"):
            raise BufferAPIError(f"Buffer createPost rejected request: {str(result['message'])[:500]}")
        if not result.get("post"):
            raise BufferAPIError("Buffer createPost returned no post")
        return result

    @staticmethod
    def _map_post_status(status: Any) -> str:
        normalized = str(status or "").lower()
        if normalized in {"sent", "published"}:
            return "PUBLISHED"
        if normalized in {"error", "failed"}:
            return "FAILED"
        return "QUEUED"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, (BufferAPIError, PublicationBlocked)):
            return str(exc)
        return f"{type(exc).__name__}: Buffer request failed"

    def _select_channels(self, channels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for network, configured_id in (
            ("instagram", self.config.instagram_channel_id),
            ("tiktok", self.config.tiktok_channel_id),
            ("youtube", self.config.youtube_channel_id),
        ):
            if network not in self.config.networks:
                continue
            candidates = [item for item in channels if _channel_service(item) == network]
            if configured_id:
                exact = [item for item in candidates if item["id"] == configured_id]
                if len(exact) != 1:
                    raise PublicationBlocked(f"configured Buffer {network} channel ID was not discovered")
                selected[network] = exact[0]
                continue
            named = [item for item in candidates if self.config.brand_name.lower() in item["name"].lower()]
            if len(named) == 1:
                selected[network] = named[0]
            elif len(candidates) == 1:
                selected[network] = candidates[0]
            elif not candidates:
                raise PublicationBlocked(f"Buffer returned no connected {network} channel")
            else:
                raise PublicationBlocked(
                    f"Buffer returned multiple {network} channels; set BUFFER_{network.upper()}_CHANNEL_ID explicitly"
                )
        return selected

    def _selection_basis(self, channels: list[dict[str, Any]], selected: Mapping[str, dict[str, Any]]) -> str:
        if self.config.instagram_channel_id or self.config.tiktok_channel_id:
            return "explicit_channel_id_verified_against_discovery"
        if all(self.config.brand_name.lower() in item["name"].lower() for item in selected.values()):
            return "unique_clip_radar_brand_name_match"
        return "unique_channel_per_required_service"


def _is_public_media_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not value.startswith("<")


def _safe_media_delivery(delivery: Mapping[str, Any] | None) -> dict[str, Any]:
    if not delivery:
        return {}
    allowed = {
        "provider",
        "status",
        "public_id",
        "public_url",
        "secure_url",
        "source_sha256",
        "source_file_size",
        "hosted_bytes",
        "duration",
        "width",
        "height",
        "format",
        "resource_type",
        "uploaded_at",
        "cleanup_after",
        "verified_at",
        "verification",
    }
    return {key: delivery[key] for key in allowed if key in delivery}


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
