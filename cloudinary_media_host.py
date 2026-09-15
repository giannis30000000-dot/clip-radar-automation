"""Cloudinary delivery bridge for already publishable Clip Radar videos.

Cloudinary is deliberately used only as a temporary public delivery layer for
Buffer.  The adapter signs server-side Upload API requests, verifies the
resulting public URL with a small ranged probe, reuses verified uploads, and
records only non-secret delivery metadata in the publication ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from publication_state import PublicationLedger


class MediaDeliveryBlocked(RuntimeError):
    """Raised when Cloudinary delivery cannot safely proceed."""


class CloudinaryAPIError(RuntimeError):
    """Raised for an unusable Cloudinary response."""


ACTIVE_MEDIA_STATUSES = {
    "UPLOADING",
    "UPLOADED",
    "VERIFIED",
    "READY_FOR_BUFFER",
    "BUFFER_QUEUED",
    "BUFFER_PUBLISHED",
    "CLEANUP_PENDING",
}


@dataclass(frozen=True)
class CloudinaryConfig:
    cloud_name: str | None
    api_key: str | None
    api_secret: str | None
    folder: str
    retention_hours: int
    abandoned_retention_hours: int
    max_upload_bytes: int
    max_active_objects: int
    max_cleanup_per_run: int
    api_url: str = "https://api.cloudinary.com"

    @classmethod
    def from_env(cls) -> "CloudinaryConfig":
        return cls(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME") or None,
            api_key=os.getenv("CLOUDINARY_API_KEY") or None,
            api_secret=os.getenv("CLOUDINARY_API_SECRET") or None,
            folder=_safe_folder(os.getenv("CLOUDINARY_FOLDER", "clipradar/buffer")),
            retention_hours=max(1, int(os.getenv("CLOUDINARY_RETENTION_HOURS", "48"))),
            abandoned_retention_hours=max(
                1, int(os.getenv("CLOUDINARY_ABANDONED_RETENTION_HOURS", "24"))
            ),
            max_upload_bytes=max(
                1, int(os.getenv("CLOUDINARY_MAX_UPLOAD_BYTES", str(75 * 1024 * 1024)))
            ),
            max_active_objects=max(1, int(os.getenv("CLOUDINARY_MAX_ACTIVE_OBJECTS", "8"))),
            max_cleanup_per_run=max(1, int(os.getenv("CLOUDINARY_MAX_CLEANUP_PER_RUN", "20"))),
        )

    def missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self.cloud_name:
            missing.append("CLOUDINARY_CLOUD_NAME")
        if not self.api_key:
            missing.append("CLOUDINARY_API_KEY")
        if not self.api_secret:
            missing.append("CLOUDINARY_API_SECRET")
        return missing

    def safe_summary(self) -> dict[str, Any]:
        return {
            "provider": "cloudinary",
            "cloud_name_configured": bool(self.cloud_name),
            "credentials_configured": not self.missing_credentials(),
            "credential_names": [
                "CLOUDINARY_CLOUD_NAME",
                "CLOUDINARY_API_KEY",
                "CLOUDINARY_API_SECRET",
            ],
            "folder": self.folder,
            "retention_hours": self.retention_hours,
            "abandoned_retention_hours": self.abandoned_retention_hours,
            "max_upload_bytes": self.max_upload_bytes,
            "max_active_objects": self.max_active_objects,
            "max_cleanup_per_run": self.max_cleanup_per_run,
        }


def _safe_folder(value: str) -> str:
    folder = "/".join(part for part in value.strip().split("/") if part)
    if not folder or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in folder.split("/")):
        raise ValueError("CLOUDINARY_FOLDER must contain only safe path components")
    return folder


def _safe_public_id(clip_id: str, folder: str) -> str:
    safe_clip_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(clip_id)).strip("-.")
    if not safe_clip_id:
        raise MediaDeliveryBlocked("Cloudinary public ID requires a non-empty Twitch clip ID")
    return f"{folder}/{safe_clip_id}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_headers(response: requests.Response) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in response.headers.items()
        if "rate" in key.lower() or key.lower() in {"retry-after"}
    }


def _safe_url(url: Any) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.netloc:
        raise MediaDeliveryBlocked("Cloudinary returned a non-HTTPS public delivery URL")
    return str(url)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(params: Mapping[str, Any], api_secret: str) -> str:
    canonical = "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if params[key] is not None and str(params[key]) != ""
    )
    return hashlib.sha1(f"{canonical}{api_secret}".encode("utf-8")).hexdigest()


def _redact_error(value: Any, config: CloudinaryConfig) -> str:
    message = str(value or "Cloudinary request failed")
    for secret in (config.api_key, config.api_secret, config.cloud_name):
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:500]


class CloudinaryMediaHost:
    def __init__(
        self,
        config: CloudinaryConfig | None = None,
        session: requests.Session | None = None,
        clock: Any | None = None,
    ):
        self.config = config or CloudinaryConfig.from_env()
        self.session = session or requests.Session()
        self.clock = clock or _utc_now
        self.api_calls = 0
        self.rate_limits: list[dict[str, str]] = []

    def upload_final(
        self,
        final_path: Path,
        clip_id: str,
        ledger: PublicationLedger,
    ) -> dict[str, Any]:
        """Upload and externally verify one final MP4, returning safe metadata."""

        self._require_credentials()
        if not final_path.exists() or not final_path.is_file():
            raise MediaDeliveryBlocked(f"final MP4 does not exist: {final_path}")
        file_size = final_path.stat().st_size
        if file_size <= 0:
            raise MediaDeliveryBlocked("final MP4 is empty")
        if file_size > self.config.max_upload_bytes:
            raise MediaDeliveryBlocked(
                f"final MP4 exceeds Cloudinary safety limit ({file_size} > {self.config.max_upload_bytes} bytes)"
            )

        source_hash = _sha256(final_path)
        existing = (ledger.get(str(clip_id)) or {}).get("media_delivery") or {}
        if (
            existing.get("provider") == "cloudinary"
            and existing.get("source_sha256") == source_hash
            and existing.get("status") in ACTIVE_MEDIA_STATUSES
            and existing.get("public_url")
            and existing.get("public_id")
        ):
            try:
                verification = self.verify_public_url(existing["public_url"])
            except Exception:
                verification = None
            if verification:
                refreshed = dict(existing)
                refreshed["status"] = "VERIFIED"
                refreshed["verification"] = verification
                refreshed["verified_at"] = _iso(self.clock())
                ledger.update_media_delivery(str(clip_id), refreshed)
                return refreshed

        public_id = _safe_public_id(str(clip_id), self.config.folder)
        if self._active_object_count(ledger, str(clip_id)) >= self.config.max_active_objects:
            raise MediaDeliveryBlocked(
                f"Cloudinary active-object safety limit reached ({self.config.max_active_objects})"
            )

        started = self.clock()
        provisional = {
            "provider": "cloudinary",
            "status": "UPLOADING",
            "public_id": public_id,
            "source_sha256": source_hash,
            "source_file_size": file_size,
            "uploaded_at": _iso(started),
            "cleanup_after": _iso(started + timedelta(hours=self.config.abandoned_retention_hours)),
        }
        ledger.update_media_delivery(str(clip_id), provisional)
        try:
            payload = self._upload(final_path, public_id)
            secure_url = _safe_url(payload.get("secure_url"))
            returned_public_id = str(payload.get("public_id") or public_id)
            verification = self.verify_public_url(secure_url)
            delivered_at = self.clock()
            result = {
                "provider": "cloudinary",
                "status": "READY_FOR_BUFFER",
                "public_id": returned_public_id,
                "public_url": secure_url,
                "secure_url": secure_url,
                "source_sha256": source_hash,
                "source_file_size": file_size,
                "hosted_bytes": _optional_int(payload.get("bytes")),
                "duration": _optional_float(payload.get("duration")),
                "width": _optional_int(payload.get("width")),
                "height": _optional_int(payload.get("height")),
                "format": str(payload.get("format") or "mp4"),
                "resource_type": str(payload.get("resource_type") or "video"),
                "uploaded_at": _iso(delivered_at),
                "cleanup_after": _iso(delivered_at + timedelta(hours=self.config.retention_hours)),
                "verified_at": _iso(delivered_at),
                "verification": verification,
            }
            ledger.update_media_delivery(str(clip_id), result)
            return result
        except Exception as exc:
            failed = dict(provisional)
            failed["status"] = "FAILED"
            failed["error"] = _redact_error(exc, self.config)
            failed["cleanup_after"] = _iso(
                self.clock() + timedelta(hours=self.config.abandoned_retention_hours)
            )
            ledger.update_media_delivery(str(clip_id), failed)
            if isinstance(exc, (MediaDeliveryBlocked, CloudinaryAPIError)):
                raise
            raise MediaDeliveryBlocked(_redact_error(exc, self.config)) from exc

    def verify_public_url(self, public_url: str) -> dict[str, Any]:
        """Verify HTTPS delivery and probe the first bytes as an MP4."""

        url = _safe_url(public_url)
        try:
            head = self.session.head(url, timeout=30, allow_redirects=True)
            self.api_calls += 1
            self._record_rate_limits(head)
            if not 200 <= head.status_code < 400:
                raise CloudinaryAPIError(f"Cloudinary public URL HEAD returned HTTP {head.status_code}")
            probe = self.session.get(
                url,
                headers={"Range": "bytes=0-4095"},
                timeout=30,
                allow_redirects=True,
                stream=True,
            )
            self.api_calls += 1
            self._record_rate_limits(probe)
            if probe.status_code not in {200, 206}:
                raise CloudinaryAPIError(f"Cloudinary public URL probe returned HTTP {probe.status_code}")
            prefix = b""
            try:
                for chunk in probe.iter_content(chunk_size=4096):
                    prefix += chunk
                    if len(prefix) >= 4096:
                        break
            finally:
                close = getattr(probe, "close", None)
                if close:
                    close()
            content_type = str(probe.headers.get("Content-Type") or head.headers.get("Content-Type") or "")
            if "video" not in content_type.lower() and "mp4" not in content_type.lower():
                raise CloudinaryAPIError("Cloudinary public URL did not return a video content type")
            if b"ftyp" not in prefix[:128]:
                raise CloudinaryAPIError("Cloudinary public URL probe did not contain an MP4 signature")
            final_url = _safe_url(getattr(probe, "url", None) or getattr(head, "url", None) or url)
            return {
                "status": "PUBLIC_HTTPS_MP4_VERIFIED",
                "head_status": int(head.status_code),
                "probe_status": int(probe.status_code),
                "content_type": content_type,
                "content_length": _optional_int(
                    probe.headers.get("Content-Length") or head.headers.get("Content-Length")
                ),
                "probe_bytes": len(prefix),
                "url": final_url,
                "checked_at": _iso(self.clock()),
                "rate_limits": _safe_headers(probe) or _safe_headers(head),
            }
        except (MediaDeliveryBlocked, CloudinaryAPIError):
            raise
        except Exception as exc:
            raise CloudinaryAPIError("Cloudinary public URL verification failed") from exc

    def mark_buffer_result(
        self,
        clip_id: str,
        result: Mapping[str, Any],
        ledger: PublicationLedger,
    ) -> dict[str, Any] | None:
        media = dict((ledger.get(str(clip_id)) or {}).get("media_delivery") or {})
        if not media or media.get("provider") != "cloudinary":
            return None
        networks = result.get("networks") or {}
        statuses = [str(item.get("status") or "") for item in networks.values()]
        now = self.clock()
        if statuses and all(status in {"QUEUED", "PUBLISHED"} for status in statuses):
            media["status"] = "BUFFER_QUEUED" if "QUEUED" in statuses else "BUFFER_PUBLISHED"
            media["buffer_ingestion_verified_at"] = _iso(now)
            media["cleanup_after"] = _iso(now + timedelta(hours=self.config.retention_hours))
        else:
            media["status"] = "BUFFER_PARTIAL_OR_FAILED"
            media["cleanup_after"] = _iso(
                now + timedelta(hours=self.config.retention_hours if any(status in {"QUEUED", "PUBLISHED"} for status in statuses) else self.config.abandoned_retention_hours)
            )
        ledger.update_media_delivery(str(clip_id), media)
        return media

    def cleanup_expired(
        self,
        ledger: PublicationLedger,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Delete only expired, non-active bridge assets with bounded work."""

        self._require_credentials()
        now = now or self.clock()
        candidates: list[tuple[str, dict[str, Any]]] = []
        for clip_id, record in ledger.iter_clips():
            media = dict(record.get("media_delivery") or {})
            if media.get("provider") != "cloudinary" or media.get("status") == "DELETED":
                continue
            cleanup_after = _parse_datetime(media.get("cleanup_after"))
            if not cleanup_after or cleanup_after > now:
                continue
            networks = record.get("networks") or {}
            if any(
                str(network.get("status") or "") in {"QUEUED", "PUBLISHING"}
                for network in networks.values()
            ):
                continue
            candidates.append((str(clip_id), media))

        results: list[dict[str, Any]] = []
        for clip_id, media in candidates[: self.config.max_cleanup_per_run]:
            try:
                payload = self._destroy(str(media.get("public_id") or ""))
                deleted = dict(media)
                deleted.update(
                    {
                        "status": "DELETED",
                        "deleted_at": _iso(now),
                        "delete_result": str(payload.get("result") or "ok"),
                    }
                )
                ledger.update_media_delivery(clip_id, deleted)
                results.append({"clip_id": clip_id, "status": "DELETED", "public_id": media.get("public_id")})
            except Exception as exc:
                results.append(
                    {
                        "clip_id": clip_id,
                        "status": "FAILED",
                        "public_id": media.get("public_id"),
                        "error": _redact_error(exc, self.config),
                    }
                )
        return {
            "provider": "cloudinary",
            "status": "READY" if not any(item["status"] == "FAILED" for item in results) else "PARTIAL_FAILURE",
            "candidates": len(candidates),
            "processed": len(results),
            "results": results,
            "api_calls": self.api_calls,
            "rate_limits": list(self.rate_limits),
            "configuration": self.config.safe_summary(),
        }

    def _require_credentials(self) -> None:
        missing = self.config.missing_credentials()
        if missing:
            raise MediaDeliveryBlocked("missing Cloudinary configuration: " + ", ".join(missing))

    def _active_object_count(self, ledger: PublicationLedger, current_clip_id: str) -> int:
        count = 0
        for clip_id, record in ledger.iter_clips():
            if clip_id == current_clip_id:
                continue
            media = record.get("media_delivery") or {}
            if media.get("provider") == "cloudinary" and media.get("status") in ACTIVE_MEDIA_STATUSES:
                count += 1
        return count

    def _upload(self, final_path: Path, public_id: str) -> dict[str, Any]:
        timestamp = int(self.clock().timestamp())
        signed_params = {
            "overwrite": "true",
            "public_id": public_id,
            "timestamp": str(timestamp),
            "type": "upload",
        }
        data = dict(signed_params)
        data.update(
            {
                "api_key": self.config.api_key,
                "signature": _signature(signed_params, self.config.api_secret or ""),
            }
        )
        url = f"{self.config.api_url}/v1_1/{self.config.cloud_name}/video/upload"
        try:
            with final_path.open("rb") as video_file:
                response = self.session.post(
                    url,
                    data=data,
                    files={"file": (final_path.name, video_file, "video/mp4")},
                    timeout=300,
                )
            self.api_calls += 1
            self._record_rate_limits(response)
            if not response.ok:
                raise CloudinaryAPIError(f"Cloudinary upload failed with HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise CloudinaryAPIError("Cloudinary upload returned invalid JSON")
            if payload.get("error"):
                error = payload.get("error")
                message = error.get("message") if isinstance(error, dict) else error
                raise CloudinaryAPIError(f"Cloudinary upload rejected: {str(message)[:300]}")
            if not payload.get("secure_url") or not payload.get("public_id"):
                raise CloudinaryAPIError("Cloudinary upload returned no secure URL or public ID")
            return payload
        except (CloudinaryAPIError, MediaDeliveryBlocked):
            raise
        except requests.RequestException as exc:
            raise CloudinaryAPIError("Cloudinary upload request failed") from exc

    def _destroy(self, public_id: str) -> dict[str, Any]:
        if not public_id:
            raise MediaDeliveryBlocked("cannot delete Cloudinary media without a public ID")
        timestamp = int(self.clock().timestamp())
        signed_params = {"public_id": public_id, "timestamp": str(timestamp), "type": "upload"}
        data = dict(signed_params)
        data.update(
            {
                "api_key": self.config.api_key,
                "signature": _signature(signed_params, self.config.api_secret or ""),
            }
        )
        url = f"{self.config.api_url}/v1_1/{self.config.cloud_name}/video/destroy"
        try:
            response = self.session.post(url, data=data, timeout=60)
            self.api_calls += 1
            self._record_rate_limits(response)
            if not response.ok:
                raise CloudinaryAPIError(f"Cloudinary destroy failed with HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise CloudinaryAPIError("Cloudinary destroy returned invalid JSON")
            result = str(payload.get("result") or "")
            if result not in {"ok", "not found"}:
                raise CloudinaryAPIError("Cloudinary destroy did not confirm deletion")
            return payload
        except (CloudinaryAPIError, MediaDeliveryBlocked):
            raise
        except requests.RequestException as exc:
            raise CloudinaryAPIError("Cloudinary destroy request failed") from exc

    def _record_rate_limits(self, response: Any) -> None:
        headers = _safe_headers(response)
        if headers:
            self.rate_limits.append(headers)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


def write_cleanup_summary(path: Path, summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
