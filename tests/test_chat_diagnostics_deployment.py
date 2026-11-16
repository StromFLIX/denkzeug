import pytest

from scripts import check_chat_diagnostics_deployment as smoke


class API:
    def __init__(self):
        self.calls = []

    def call(self, path, *, timeout):
        self.calls.append((path, timeout))
        resource = path.split("/")[-1].split("?")[0]
        return {resource: [], "next_offset": None}


def test_latency_smoke_bounds_reads_and_returns_only_performance_metadata(monkeypatch):
    api = API()
    times = iter([value for index in range(6) for value in (index, index + 0.1)])
    monkeypatch.setattr(smoke, "monotonic", lambda: next(times))
    result = smoke.check(api)
    assert len(result) == 6
    assert api.calls == [
        (f"/api/chat-inspection/{resource}?limit={limit}", 3.0)
        for resource in ("chats", "sessions")
        for limit in (1, 20, 50)
    ]
    assert all(row["seconds"] == 0.1 for row in result)
    assert all(set(row) == {"resource", "limit", "rows", "seconds"} for row in result)


def test_latency_smoke_rejects_slow_local_metadata(monkeypatch):
    times = iter([0, 3])
    monkeypatch.setattr(smoke, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="latency budget"):
        smoke.check(API())


def test_latency_smoke_rejects_unbounded_or_invalid_pages(monkeypatch):
    monkeypatch.setattr(API, "call", lambda *args, **kwargs: {"chats": [None, None], "next_offset": 2})
    with pytest.raises(RuntimeError, match="invalid bounded page"):
        smoke.check(API())
