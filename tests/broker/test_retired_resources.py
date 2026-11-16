"""Retirement cleanup must never prune another stack or shared Docker state."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from nautionette_docker_broker import retired_resources as retired


@pytest.fixture
def runtime(monkeypatch):
    docker = MagicMock()
    broker = SimpleNamespace(id="broker", labels={"com.docker.compose.project": "our-stack"})
    docker.containers.get.return_value = broker
    for kind in ("containers", "networks", "images", "volumes"):
        getattr(docker, kind).list.return_value = []
    monkeypatch.setattr(retired.daemon, "client", lambda: docker)
    monkeypatch.setattr(retired, "WORKFLOWS_VOLUME", "our-workflows")
    monkeypatch.setattr(retired, "_complete", False)
    return docker, broker


def resource(kind, deployment="our-workflows"):
    labels = {"nautionette.miniapp": "old", "nautionette.deployment": deployment}
    item = MagicMock(id=kind, labels=labels, attrs={"Labels": labels, "Containers": {"broker": {}}})
    return item


def test_cleanup_removes_only_owned_retired_resources_and_is_idempotent(runtime):
    docker, broker = runtime
    owned, other = {}, {}
    for kind in ("containers", "networks", "images"):
        owned[kind], other[kind] = resource(kind), resource(kind, "other-workflows")
        getattr(docker, kind).list.return_value = [owned[kind], other[kind]]
    volume = MagicMock(
        attrs={"Labels": {"com.docker.compose.project": "our-stack", "com.docker.compose.volume": "miniapps"}}
    )
    other_volume = MagicMock(
        attrs={
            "Labels": {"com.docker.compose.project": "other-stack", "com.docker.compose.volume": "miniapps"}
        }
    )
    unrelated_volume = MagicMock(
        attrs={
            "Labels": {"com.docker.compose.project": "our-stack", "com.docker.compose.volume": "backend-data"}
        }
    )
    docker.volumes.list.return_value = [volume, other_volume, unrelated_volume]
    retired.reconcile()
    retired.reconcile()
    owned["containers"].remove.assert_called_once_with(force=True)
    owned["networks"].disconnect.assert_called_once_with(broker, force=True)
    owned["networks"].remove.assert_called_once_with()
    docker.images.remove.assert_called_once_with("images", force=False)
    volume.remove.assert_called_once_with(force=False)
    other_volume.remove.assert_not_called()
    unrelated_volume.remove.assert_not_called()
    for item in other.values():
        item.remove.assert_not_called()
        item.disconnect.assert_not_called()
    for kind in ("containers", "networks", "images"):
        assert getattr(docker, kind).list.call_args.kwargs["filters"] == {
            "label": ["nautionette.miniapp", "nautionette.deployment=our-workflows"]
        }
        assert getattr(docker, kind).list.call_count == 1


def test_failed_cleanup_retries_without_forcing_an_in_use_volume(runtime):
    docker, _ = runtime
    volume = MagicMock(
        attrs={"Labels": {"com.docker.compose.project": "our-stack", "com.docker.compose.volume": "miniapps"}}
    )
    volume.remove.side_effect = [RuntimeError("volume still mounted"), None]
    docker.volumes.list.return_value = [volume]
    with pytest.raises(RuntimeError, match="still mounted"):
        retired.reconcile()
    assert not retired._complete
    retired.reconcile()
    assert retired._complete
    assert all(call.kwargs == {"force": False} for call in volume.remove.call_args_list)


def test_unknown_stack_identity_fails_closed(runtime):
    docker, broker = runtime
    broker.labels.clear()
    with pytest.raises(RuntimeError, match="stack identity"):
        retired.reconcile()
    docker.containers.list.assert_not_called()
    docker.volumes.list.assert_not_called()
    assert not retired._complete
