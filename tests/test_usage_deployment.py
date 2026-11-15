"""Staging checks stay read-only, credential-free in logs and internally consistent."""

import io
import json

import pytest

from scripts.check_usage_deployment import check, validate


def sample():
    tokens = {"input": 10, "output": 3, "cacheRead": 2, "cacheWrite": 1, "total": 16}
    return {
        "summary": {"tokens": tokens, "executions": 1, "responses": 1, "reports": 1},
        "daily": [{"tokens": tokens, "chats": tokens, "workflows": None}],
        "rows": [{"tokens": tokens}],
    }


def test_staging_metric_checks_reconcile_and_accept_unknown_usage():
    validate(sample())
    validate(
        {"summary": {"tokens": None, "executions": 1, "responses": 1, "reports": 0}, "daily": [], "rows": []}
    )
    broken = sample()
    broken["summary"]["tokens"]["total"] = 17
    with pytest.raises(ValueError, match="reconcile"):
        validate(broken)


def test_probe_calls_only_readonly_usage_routes():
    class Session:
        base = "https://stage.example.test"
        requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            export = "/export?" in request.full_url
            response = io.BytesIO(b"start,end,timezone,id\r\n" if export else json.dumps(sample()).encode())
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
