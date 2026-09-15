"""Select the configured publication backend without coupling the pipeline to it."""

from __future__ import annotations

import os
from typing import Any


def configured_backend() -> str:
    return os.getenv("PUBLISHER_BACKEND", "none").strip().lower()


def create_publisher() -> Any | None:
    backend = configured_backend()
    if backend in {"", "none", "disabled"}:
        return None
    if backend == "buffer":
        from buffer_publisher import BufferPublisher

        return BufferPublisher()
    if backend == "metricool":
        from metricool_publisher import MetricoolPublisher

        return MetricoolPublisher()
    raise RuntimeError(f"unsupported publisher backend: {backend}")
