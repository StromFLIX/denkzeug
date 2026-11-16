"""HTTP MCP gateway ceiling, separate from the agent's per-request deadline.

agentgateway v1.7 defaults internal HTTP calls to 10 seconds. Its target HTTP
policy (not the LLM/outer route timeout) controls this hop. Leave room for the
agent's maximum 600-second request deadline to fire first and report accurately.
"""

from copy import deepcopy
from typing import Any

GATEWAY_REQUEST_TIMEOUT = "615s"


def with_http_timeout(target: dict[str, Any]) -> dict[str, Any]:
    """Default only missing HTTP settings; preserve explicit administrator limits.

    Stdio targets do not support HTTP policies. Never rebuild a target or drop
    credentials, conditions, TLS settings, transformations, or other policies.
    """
    value = deepcopy(target)
    if isinstance(value.get("mcp"), dict):
        policies = value["policies"] = value.get("policies") or {}
        http = policies["http"] = policies.get("http") or {}
        if http.get("requestTimeout") is None:
            http["requestTimeout"] = GATEWAY_REQUEST_TIMEOUT
    return value


def logical_http_timeout(target: dict[str, Any]) -> dict[str, Any]:
    """Our exact transport default is not an edit to OAuth credential ownership.

    Normalize it away from logical snapshots so upgrading an existing connected
    account never looks like an external credential/policy replacement. Leave
    every explicit non-default administrator setting intact.
    """
    value = deepcopy(target)
    policies = value.get("policies") or {}
    http = policies.get("http") or {}
    if http.get("requestTimeout") == GATEWAY_REQUEST_TIMEOUT:
        del http["requestTimeout"]
        if not http:
            policies.pop("http", None)
        if not policies:
            value.pop("policies", None)
    return value
