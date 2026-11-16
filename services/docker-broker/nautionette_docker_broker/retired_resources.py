"""One-time, deployment-scoped cleanup of retired execution resources on upgrade.

No caller-controlled identifiers, runtime management API or global Docker prune.
Failures retry on the next monitor pass; do not mark a partial cleanup complete.
"""

from __future__ import annotations

import socket

from . import daemon
from .config import WORKFLOWS_VOLUME

_complete = False
_LABEL = "nautionette.miniapp"


def reconcile() -> None:
    global _complete
    if _complete:
        return
    client = daemon.client()
    broker = client.containers.get(socket.gethostname())
    project = broker.labels.get("com.docker.compose.project")
    if not project:
        raise RuntimeError("Refusing retired resource cleanup without broker stack identity")
    deployment = {"nautionette.deployment": WORKFLOWS_VOLUME}
    filters = {"label": [_LABEL, f"nautionette.deployment={WORKFLOWS_VOLUME}"]}

    def owned(labels):
        return bool(labels.get(_LABEL)) and all(labels.get(k) == v for k, v in deployment.items())

    for container in client.containers.list(all=True, filters=filters):
        if owned(container.labels):
            container.remove(force=True)
    for network in client.networks.list(filters=filters):
        if owned(network.attrs.get("Labels") or {}):
            # Old networks may still have the broker's SDK callback endpoint.
            if broker.id in (network.attrs.get("Containers") or {}):
                network.disconnect(broker, force=True)
            network.remove()  # Never force-disconnect another container.
    for image in client.images.list(filters=filters):
        if owned(image.labels):
            client.images.remove(image.id, force=False)
    volume_labels = {"com.docker.compose.project": project, "com.docker.compose.volume": "miniapps"}
    for volume in client.volumes.list(filters={"label": [f"{k}={v}" for k, v in volume_labels.items()]}):
        labels = volume.attrs.get("Labels") or {}
        if all(labels.get(k) == v for k, v in volume_labels.items()):
            volume.remove(force=False)  # In-use volumes fail closed, never remove unrelated state.
    _complete = True
    daemon.log.info("retired resource cleanup complete")
