"""Bill data — Cost Explorer month-to-date + projection, cached daily.

One CE query per account per day (~$0.01 each, data lags ~24 h), cached in
the app data dir; the page always renders from cache with its age labelled.
The projection is local arithmetic (MTD extrapolated over the month), and
budget thresholds are evaluated locally too — no AWS Budgets resource is
ever created (an account-level resource a non-technical user would then own
without knowing it exists).

CALLER CONTRACT: handlers gate with ``refuse_and_log(SERVICE_COST_EXPLORER)``
before calling. Sync, subprocess-bound — call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.deploy.engine import _checked

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
_CACHE_TTL_SECS = 24 * 3600


def _cache_path(account: str) -> Path:
    # Account ids are 12 digits (validated upstream); defensive strip anyway.
    safe = "".join(c for c in account if c.isdigit())[:16] or "unknown"
    return app_data_dir(APP_NAME) / "costs" / f"{safe}.json"


def read_cached(account: str) -> Optional[dict[str, Any]]:
    """The cached bill for ``account`` regardless of age (age is labelled)."""
    try:
        return json.loads(_cache_path(account).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def is_fresh(cached: Optional[dict[str, Any]]) -> bool:
    if not cached:
        return False
    try:
        fetched = dt.datetime.fromisoformat(cached["fetchedAt"])
    except (KeyError, ValueError):
        return False
    age = dt.datetime.now(dt.timezone.utc) - fetched
    return age.total_seconds() < _CACHE_TTL_SECS


def fetch_month_costs(profile: str, region: str, account: str) -> dict[str, Any]:
    """Query CE for this month's spend, grouped by service; write the cache.

    CE is a global endpoint (region-independent); the profile decides the
    account. Amounts are UnblendedCost in USD, rounded to cents for display —
    the raw strings stay in the cache for anything that needs precision.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today.replace(day=1)
    # CE's End is exclusive; asking through tomorrow includes today's partial.
    end = today + dt.timedelta(days=1)
    out = _checked(
        [
            "ce",
            "get-cost-and-usage",
            "--time-period",
            f"Start={start.isoformat()},End={end.isoformat()}",
            "--granularity",
            "MONTHLY",
            "--metrics",
            "UnblendedCost",
            "--group-by",
            "Type=DIMENSION,Key=SERVICE",
            "--output",
            "json",
        ],
        profile,
        action="ce:GetCostAndUsage",
        timeout=60,
    )
    data = json.loads(out or "{}")
    by_service: list[dict[str, Any]] = []
    total = 0.0
    for period in data.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount <= 0:
                continue
            total += amount
            by_service.append(
                {"service": (group.get("Keys") or ["?"])[0], "amount": round(amount, 2)}
            )
    by_service.sort(key=lambda row: -row["amount"])
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    elapsed = max(today.day, 1)
    projected = total / elapsed * days_in_month
    result = {
        "account": account,
        "monthToDate": round(total, 2),
        "projected": round(projected, 2),
        "currency": "USD",
        "byService": by_service[:12],
        "periodStart": start.isoformat(),
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    path = _cache_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(result, indent=1))
    return result
