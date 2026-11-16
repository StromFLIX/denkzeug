"""Portable workflow envelope for an app from the acting account's Settings library."""

from __future__ import annotations

import re
from typing import Any


def app_result(
    app: str, data: dict[str, Any], *, summary: str, revision: int | None = None, notify: bool = True
) -> dict[str, Any]:
    """Return data + a UI reference; the backend pins and validates at completion.

    This helper does not publish an app, execute JavaScript, or invoke a tool.
    Omit revision to resolve the current published revision for this run only.
    An old message always retains the exact revision originally resolved.
    """
    if not isinstance(app, str) or len(app) > 80 or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", app):
        raise ValueError("Use an app slug from Settings → Apps")
    if not isinstance(data, dict) or not isinstance(summary, str) or type(notify) is not bool:
        raise ValueError("Provide object data, text summary and a boolean notify flag")
    ui: dict[str, Any] = {"app": app}
    if revision is not None:
        if type(revision) is not int or revision < 1:
            raise ValueError("App revision must be a positive integer")
        ui["revision"] = revision
    return {"summary": summary, "data": data, "ui": ui, "notify": notify}
