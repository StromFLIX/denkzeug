"""The HTTP surface, one router per thing the app is about.

`frontend` is last on purpose: its catch-all would otherwise swallow the rest.
"""

from __future__ import annotations

from .. import access
from . import (
    agents,
    artcraft,
    auth,
    chat_inspection,
    chat_outputs,
    chats,
    delegation,
    frontend,
    gateway,
    github_setup,
    internal,
    miniapps,
    operational,
    pi_packages,
    projects,
    runs,
    search,
    settings,
    speech,
    storage,
    subagents,
    system,
    tokens,
    usage,
    workflows,
)

ROUTERS = (
    access.router,
    auth.router,
    tokens.router,
    system.router,
    settings.router,
    usage.router,
    storage.router,
    agents.router,
    subagents.router,
    pi_packages.router,
    artcraft.router,
    miniapps.router,
    gateway.router,
    chats.router,
    speech.router,
    chat_outputs.router,
    chat_inspection.router,
    search.router,
    workflows.router,
    runs.router,
    internal.router,
    delegation.router,
    operational.router,
    projects.router,
    github_setup.router,
    frontend.router,
)

__all__ = ["ROUTERS"]
