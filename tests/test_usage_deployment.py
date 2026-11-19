"""Staging checks stay read-only, credential-free in logs and internally consistent."""

import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.check_usage_deployment import check, validate, validate_chart


def sample():
    tokens = {"input": 10, "output": 3, "cacheRead": 2, "cacheWrite": 1, "total": 16}
    return {
        "summary": {"tokens": tokens, "executions": 1, "responses": 1, "reports": 1},
        "daily": [{"tokens": tokens, "chats": tokens, "workflows": None}],
        "rows": [{"tokens": tokens}],
    }


def grouped_sample(group):
    data = sample()
    data["chart"] = {"group": group, "series": [{"id": "sample", "label": "Sample", "total": 16}]}
    data["daily"][0]["series"] = [16]
    return data


def test_staging_metric_checks_reconcile_and_accept_unknown_usage():
    validate(sample())
    validate(
        {"summary": {"tokens": None, "executions": 1, "responses": 1, "reports": 0}, "daily": [], "rows": []}
    )
    broken = sample()
    broken["summary"]["tokens"]["total"] = 17
    with pytest.raises(ValueError, match="reconcile"):
        validate(broken)


def test_staging_grouped_series_checks_range_and_daily_reconciliation():
    data = grouped_sample("models")
    validate_chart(data, "models")
    data["chart"]["series"][0]["total"] = 15
    with pytest.raises(ValueError, match="Range chart series do not reconcile"):
        validate_chart(data, "models")
    data["daily"][0]["series"] = [15]
    with pytest.raises(ValueError, match="Daily chart series do not reconcile"):
        validate_chart(data, "models")


def test_probe_calls_only_readonly_usage_routes():
    class Session:
        base = "https://stage.example.test"
        requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            export = "/export?" in request.full_url
            if export:
                response = io.BytesIO(b"start,end,timezone,id\r\n")
            else:
                group = parse_qs(urlsplit(request.full_url).query)["chart_group"][0]
                response = io.BytesIO(json.dumps(grouped_sample(group)).encode())
            response.headers = {
                "Cache-Control": "no-store",
                "Content-Type": "text/csv" if export else "application/json",
            }
            return response

    session = Session()
    check(session)
    assert len(session.requests) == 6
    assert all(request.get_method() == "GET" for request in session.requests)
    assert all(
        request.full_url.startswith(session.base + "/api/settings/usage") for request in session.requests
    )
