"""Wire managed stdio targets through the gateway image's bounded child runner."""

from copy import deepcopy
from typing import Any

SUPERVISOR = "/app/stdio_supervisor.py"


def supervised_target(target: dict[str, Any]) -> dict[str, Any]:
    stdio = target.get("stdio")
    if not stdio or stdio.get("cmd") == SUPERVISOR:
        return target
    return {
        **target,
        "stdio": {
            **stdio,
            "cmd": SUPERVISOR,
            "args": [target["name"], "--", stdio["cmd"], *stdio.get("args", [])],
        },
    }


def logical_stdio(target: dict[str, Any]) -> dict[str, Any]:
    """Show/compare the configured launch, not the app-owned wire wrapper."""
    stdio = target.get("stdio") or {}
    args = stdio.get("args") or []
    if stdio.get("cmd") != SUPERVISOR or len(args) < 3 or args[:2] != [target.get("name"), "--"]:
        return target
    value = deepcopy(target)
    value["stdio"].update(cmd=args[2], args=args[3:])
    return value
