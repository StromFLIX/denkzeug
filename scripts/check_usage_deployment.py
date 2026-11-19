"""Authenticated, read-only staging usage checks. Never print response bodies."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from scripts.account_session import AccountSession

FIELDS = ("input", "output", "cacheRead", "cacheWrite")


def validate(data: dict) -> None:
    summary = data["summary"]
    tokens = summary["tokens"]
    if tokens is not None:
        if not all(type(tokens.get(key)) is int and tokens[key] >= 0 for key in (*FIELDS, "total")):
            raise ValueError("Invalid token totals")
        if tokens["total"] != sum(tokens[key] for key in FIELDS):
            raise ValueError("Token categories do not reconcile")
    if sum((day["tokens"] or {}).get("total", 0) for day in data["daily"]) != (tokens or {}).get("total", 0):
        raise ValueError("Daily chart does not reconcile")
    for day in data["daily"]:
        if sum((day[key] or {}).get("total", 0) for key in ("chats", "workflows")) != (
            day["tokens"] or {}
        ).get("total", 0):
            raise ValueError("Workload chart does not reconcile")
    if not all(
        type(summary[key]) is int and summary[key] >= 0 for key in ("executions", "responses", "reports")
    ):
        raise ValueError("Invalid execution counts")
    if any(row.get("tokens") is not None and row["tokens"]["total"] < 0 for row in data["rows"]):
        raise ValueError("Invalid breakdown")


def validate_chart(data: dict, group: str) -> None:
    chart = data["chart"]
    if chart["group"] != group or len(chart["series"]) > 5:
        raise ValueError("Invalid chart grouping")
    totals = [item["total"] for item in chart["series"]]
    sums = [0] * len(totals)
    for day in data["daily"]:
        values = day["series"]
        if len(values) != len(totals) or any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Invalid daily chart series")
        if sum(values) != (day["tokens"] or {}).get("total", 0):
            raise ValueError("Daily chart series do not reconcile")
        for i, value in enumerate(values):
            sums[i] += value
    if sums != totals:
        raise ValueError("Range chart series do not reconcile")


def check(session: AccountSession) -> None:
    end = datetime.now(UTC).date()
    selected = {"start": (end - timedelta(days=29)).isoformat(), "end": end.isoformat(), "timezone": "UTC"}
    for group, chart_group in (
        ("models", "models"),
        ("agents", "agents"),
        ("workflows", "workflows"),
        ("executions", "workload"),
        ("runs", "models"),
    ):
        query = urllib.parse.urlencode({**selected, "group": group, "chart_group": chart_group})
        request = urllib.request.Request(session.base + "/api/settings/usage?" + query)  # noqa: S310 - AccountSession validates origin
        with session.open(request, timeout=60) as response:
            if response.headers.get("Cache-Control") != "no-store":
                raise ValueError("Usage responses must not be cached")
            data = json.load(response)
            validate(data)
            validate_chart(data, chart_group)
    query = urllib.parse.urlencode(selected)
    request = urllib.request.Request(session.base + "/api/settings/usage/export?" + query)  # noqa: S310 - AccountSession validates origin
    with session.open(request, timeout=60) as response:
        if "text/csv" not in response.headers.get("Content-Type", ""):
            raise ValueError("Usage export is not CSV")
        if not response.read(1024).decode("utf-8-sig").startswith("start,end,timezone,"):
            raise ValueError("Usage export header is incomplete")


def main() -> int:
    base = os.environ.get("APP_URL", "http://127.0.0.1:18080")
    try:
        with AccountSession.from_env(base) as session:
            check(session)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, urllib.error.URLError):
        print(
            "Usage verification failed; check account access, endpoint readiness and metric reconciliation.",
            file=sys.stderr,
        )
        return 1
    print("Usage: authenticated breakdowns, chart reconciliation, private caching and CSV export passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
