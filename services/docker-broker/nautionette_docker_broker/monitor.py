"""Keep fixed execution resources present even after external Docker cleanup."""

from __future__ import annotations

import threading

from . import artcraft, daemon, images, packages, retired_resources, workers
from .config import RECONCILE_SECONDS


def run(stopping: threading.Event) -> None:
    while not stopping.is_set():
        for reconcile in (
            retired_resources.reconcile,
            images.reconcile,
            workers.reconcile,
            packages.reconcile,
            artcraft.reconcile,
        ):
            try:
                reconcile()
            except Exception:
                daemon.log.exception("container reconciliation failed")
        if stopping.wait(RECONCILE_SECONDS):
            return
