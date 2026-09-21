"""Small bounded HTTP helpers; errors never include bodies, headers or keys."""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse
import requests


class ProviderFailure(RuntimeError):
    def __init__(self, code, *, retryable=False, detail=None):
        super().__init__(code)
        self.retryable = retryable
        self.detail = detail


def attempts() -> int:
    return max(1, min(3, int(os.getenv("STORY_PROVIDER_MAX_ATTEMPTS", "2"))))


def endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderFailure("INVALID_HTTPS_ENDPOINT")
    return value.rstrip("/")


def request_json(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=(15, 120), allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise ProviderFailure("NETWORK_RESULT_UNKNOWN") from None
    if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
        detail = None
        try:
            error = response.json().get("error", {})
            candidate = error.get("type") or error.get("code") if isinstance(error, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate):
                detail = candidate
        except (ValueError, TypeError, AttributeError):
            pass
        raise ProviderFailure(f"HTTP_{response.status_code}", retryable=response.status_code == 429 or response.status_code >= 500, detail=detail)
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except (ValueError, TypeError):
        raise ProviderFailure("INVALID_JSON", retryable=True) from None


def rate(name: str, default: str | None):
    from .costs import money
    value = os.getenv(name) or default
    if value is None or money(value) <= 0:
        raise ProviderFailure("PRICE_CONFIGURATION_REQUIRED")
    return money(value)
