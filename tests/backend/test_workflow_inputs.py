"""Typed admission, safe reference selectors and owned history pagination."""

import pytest
from nautionette import validate_manifest


@pytest.fixture
def typed(backend):
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["preview", "publish"]},
            "agent": {"type": "string", "x_resource": {"kind": "agent"}},
            "chat": {"type": ["string", "null"], "x_resource": {"kind": "chat"}},
            "note": {"type": "string"},
            "zero": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
        "required": ["mode"],
    }
    backend.authoring.add_workflow("typed_job", manifest={"inputs": schema})
    backend.db.execute(
        "INSERT INTO agent_profiles(id,name,description,created_at,updated_at) "
        "VALUES('agent-a','Release assistant','Release automation',0,0)"
    )
    return backend


def test_typed_enum_empty_null_false_zero_round_trip(client, typed):
    value = {"mode": "preview", "note": "", "zero": 0, "flag": False, "chat": None}
    assert client.post("/api/workflows/typed_job/run", json={"input": value}).status_code == 200
    assert typed.temporal.started[-1]["input"] == value
    count = len(typed.temporal.started)
    response = client.post("/api/workflows/typed_job/run", json={"input": {"mode": "invalid"}})
    assert response.status_code == 400
    assert response.json()["detail"]["field_errors"][0]["path"] == "/mode"
    assert len(typed.temporal.started) == count


def test_options_search_exact_selection_and_deleted_reference(client, typed):
    response = client.get(
        "/api/workflows/typed_job/input-options", params={"field": "/agent", "q": "RELEASE", "limit": 1}
    ).json()
    assert response["items"] == [
        {"value": "agent-a", "label": "Release assistant", "description": "Release automation"}
    ]
    resolved = client.get(
        "/api/workflows/typed_job/input-options",
        params={"field": "/agent", "q": "no match", "selected": "agent-a"},
    ).json()
    assert not resolved["items"]
    assert resolved["selected"]["label"] == "Release assistant"
    typed.db.execute("UPDATE agent_profiles SET name='Renamed' WHERE id='agent-a'")
    assert (
        client.get(
            "/api/workflows/typed_job/input-options", params={"field": "/agent", "selected": "agent-a"}
        ).json()["selected"]["label"]
        == "Renamed"
    )
    assert (
        client.post(
            "/api/workflows/typed_job/run", json={"input": {"mode": "preview", "agent": "agent-a"}}
        ).status_code
        == 200
    )
    typed.db.execute("DELETE FROM agent_profiles WHERE id='agent-a'")
    denied = client.post(
        "/api/workflows/typed_job/run", json={"input": {"mode": "preview", "agent": "agent-a"}}
    )
    assert denied.status_code == 400
    assert "unavailable" in denied.text
    assert client.get("/api/workflows/typed_job/input-options", params={"field": "/note"}).status_code == 400
    assert (
        client.get(
            "/api/workflows/typed_job/input-options", params={"field": "/agent", "limit": 51}
        ).status_code
        == 422
    )


def test_resource_schedule_is_rejected_before_temporal(client, typed):
    response = client.post(
        "/api/workflows/typed_job/schedule",
        json={
            "frequency": "daily",
            "timezone": "Europe/Berlin",
            "at": "09:00",
            "input": {"mode": "preview"},
        },
    )
    assert response.status_code == 400
    assert "manual" in response.text
    assert not typed.temporal.schedule_specs


def test_resource_deploy_cannot_leave_an_existing_schedule_enabled(client, typed):
    from nautionette_backend.clients import temporal

    async def existing(_):
        return {"schedule_id": "existing"}

    # Only the schedule lookup is needed: refusal must precede authoring mutation.
    original = temporal.schedule
    temporal.schedule = existing
    try:
        response = client.post(
            "/api/workflows/typed_job/deploy",
            json={
                "code": "MANIFEST = "
                + repr({"inputs": typed.authoring.workflows["typed_job"]["manifest"]["inputs"]})
            },
        )
        assert response.status_code == 409
    finally:
        temporal.schedule = original


@pytest.mark.parametrize("missing", [True, False])
def test_schedule_not_found_is_distinct_from_outage(client, typed, monkeypatch, missing):
    from nautionette_backend.clients import temporal
    from temporalio.service import RPCError, RPCStatusCode

    async def unavailable(_, *, schedule_id=None):
        raise RPCError(
            "schedule unavailable", RPCStatusCode.NOT_FOUND if missing else RPCStatusCode.UNAVAILABLE, b""
        )

    monkeypatch.setattr(temporal, "schedule", unavailable)
    result = client.get("/api/workflows/typed_job").json()
    assert result["schedule"] is None
    assert result["schedule_status"] == ("available" if missing else "unavailable")
    schema = typed.authoring.workflows["typed_job"]["manifest"]
    response = client.post("/api/workflows/typed_job/deploy", json={"code": "MANIFEST = " + repr(schema)})
    assert response.status_code == (200 if missing else 503)


def test_run_pages_are_stable_owned_and_compact(client, typed):
    for index in range(4):
        typed.db.record_run(
            "typed_job", f"page-{index}", None, "manual", {"secret": "not a table column"}, created_at=100
        )
    first = client.get("/api/runs", params={"compact": True, "limit": 2}).json()
    second = client.get(
        "/api/runs", params={"compact": True, "limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert len(first["runs"]) == len(second["runs"]) == 2
    assert {row["workflow_id"] for row in first["runs"]}.isdisjoint(
        row["workflow_id"] for row in second["runs"]
    )
    assert "input" not in first["runs"][0]
    assert not second["next_cursor"]
    assert client.get("/api/runs", params={"cursor": "invalid"}).status_code == 422
    assert client.get("/api/runs", params={"limit": 101}).status_code == 422
    assert not client.get("/api/runs", params={"status": "failed", "compact": True}).json()["runs"]


@pytest.mark.parametrize(
    "field",
    [
        {"type": "integer", "x_resource": {"kind": "agent"}},
        {"type": "string", "x_resource": {"kind": "url", "endpoint": "https://example.test"}},
        {"type": "string", "enum": ["a", "b"], "x_ui": {"enum_labels": ["Only one"]}},
        {"type": "not-a-type"},
    ],
)
def test_authoring_rejects_invalid_input_contracts(field):
    manifest = {
        "schema": 1,
        "name": "typed_job",
        "inputs": {"type": "object", "properties": {"choice": field}},
        "outputs": {"type": "object"},
    }
    assert validate_manifest(manifest)
