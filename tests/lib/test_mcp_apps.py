import pytest
from nautionette.mcp_apps import app_result


def test_workflow_app_envelope_is_explicit_and_does_not_execute():
    assert app_result("release-notes", {"notes": "# Changes"}, summary="Ready", revision=2) == {
        "summary": "Ready",
        "data": {"notes": "# Changes"},
        "ui": {"app": "release-notes", "revision": 2},
        "notify": True,
    }
    assert app_result("report", {}, summary="Nothing", notify=False)["notify"] is False
    assert "revision" not in app_result("report", {}, summary="Ready")["ui"]


@pytest.mark.parametrize(
    "app,revision", [("Bad", None), ("../other", None), ("a-", None), ("a", True), ("a", 0)]
)
def test_bad_references_fail_early(app, revision):
    with pytest.raises(ValueError):
        app_result(app, {}, summary="Ready", revision=revision)
