"""Per-video spending reservations. Unknown/failed requests never get refunded."""
from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import re

from .history import write_json


class BudgetExceeded(RuntimeError):
    pass


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Cost limits/rates must be finite non-negative numbers")
    return result


def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if not re.search(r"(?i)^(authorization|headers|api_key|api_secret|xi-api-key|token|audio_base64)$", k)}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    for name, secret in os.environ.items():
        if secret and re.search(r"(?:KEY|SECRET|TOKEN)$", name.upper()):
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer <redacted>", value)
    return re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?<redacted>", value)


class CostBudget:
    def __init__(self, path: Path, limit=None):
        self.paths = [path]
        self.limit = money((os.getenv("CLIP_RADAR_MAX_COST_USD_PER_VIDEO") or "0") if limit is None else limit)
        self.requests = []
        self.events = []
        self.stopped = False
        self.save()

    @property
    def reserved(self):
        return sum((money(r["reserved_usd"]) for r in self.requests), Decimal(0))

    def reserve(self, provider, model, cost, *, retry=0, **units):
        estimate = money(cost)
        if estimate <= 0:
            raise ValueError("Paid requests require a positive price estimate")
        if self.stopped or self.reserved + estimate > self.limit:
            self.stopped = True
            self.event("budget", "BUDGET_EXCEEDED", requested_usd=float(estimate))
            raise BudgetExceeded("BUDGET_EXCEEDED: paid generation stopped before the next request")
        record = {"provider": provider, "model": model, "reserved_usd": str(estimate), "usage_estimated_usd": None, "actual_usd": None, "retry": retry, "status": "RESERVED", **units}
        self.requests.append(sanitize(record))
        self.save()  # Reserve before any network request, including retries.
        return len(self.requests) - 1

    def update(self, index, **fields):
        self.requests[index].update(sanitize(fields))
        self.save()

    def event(self, provider, reason, **fields):
        self.events.append(sanitize({"provider": provider, "reason": reason, **fields}))
        self.save()

    def attach(self, directory: Path):
        self.paths.append(directory / "cost_report.json")
        self.save()

    def report(self):
        groups = {}
        for request in self.requests:
            key = request["provider"] + "/" + request["model"]
            group = groups.setdefault(key, {"provider": request["provider"], "model": request["model"], "requests": 0, "retries": 0, "requested_seconds": 0, "generated_seconds": 0, "generated_assets": 0, "estimated_usd": 0.0})
            group["requests"] += 1
            group["retries"] += int(request["retry"] > 0)
            for field in ("requested_seconds", "generated_seconds", "generated_assets"):
                group[field] += request.get(field, 0)
            group["estimated_usd"] = round(group["estimated_usd"] + float(request["reserved_usd"]), 8)
        return {
            "version": 1, "status": "BUDGET_EXCEEDED" if self.stopped else "WITHIN_BUDGET",
            "limit_usd": float(self.limit), "total_estimated_cost_usd": float(self.reserved),
            "remaining_budget_usd": float(max(Decimal(0), self.limit - self.reserved)),
            "actual_cost_usd": 0.0 if not self.requests else None,
            "accounting_basis": "Conservative request reservations at configured rates; no assumed refunds; actual invoice not available",
            "providers": list(groups.values()), "requests": self.requests, "events": self.events,
        }

    def save(self):
        report = sanitize(self.report())
        for path in self.paths:
            write_json(path, report)
