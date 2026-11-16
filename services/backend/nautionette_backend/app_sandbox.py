"""Standalone credential-free MCP Apps sandbox listener. Never mount in the API app.

Only this service may serve the proxy that loads executable app HTML. It has no
DB, credentials, application routes, or volumes; its CSP is an HTTP boundary,
not a removable meta element inside user HTML.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROXY = Path(__file__).with_name("data").joinpath("app-sandbox.js").read_text()
DOCUMENT = (
    '<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
    "<style>html,body{margin:0;height:100%;overflow:hidden;background:transparent}</style></head>"
    f"<body><script>{PROXY}</script></body></html>"
).encode()


def origin(value: str, *, local: bool = True) -> str:
    """Only exact web origins; never CSP directives, wildcards, credentials or paths."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise ValueError("Invalid web origin") from None
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(c in value for c in " \t\r\n;'\"<>\\*")
        or (
            parsed.scheme != "https"
            and not (
                local and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("Expected HTTPS origin (HTTP allowed only on loopback)")
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{hostname}" + (f":{port}" if port and port != default else "")


def approved_origins() -> set[str]:
    configured = os.environ.get("MCP_APPS_HOST_ORIGINS", "") or os.environ.get("DENKZEUG_PUBLIC_URL", "")
    return {origin(item.strip()) for item in configured.split(",") if item.strip()}


def sandbox_policy(parent: str, csp: dict) -> dict[str, str]:
    if parent not in approved_origins():
        raise ValueError("Embedding origin is not configured")
    allowed_keys = {"connectDomains", "resourceDomains", "frameDomains", "baseUriDomains"}
    if not isinstance(csp, dict) or set(csp) - allowed_keys:
        raise ValueError("Invalid app CSP")
    domains: dict[str, str] = {}
    for key in allowed_keys:
        values = csp.get(key, [])
        if not isinstance(values, list) or len(values) > 24 or any(not isinstance(v, str) for v in values):
            raise ValueError("Invalid app domains")
        domains[key] = " ".join(sorted({origin(v, local=False) for v in values}))
    resources = domains["resourceDomains"]
    connect = domains["connectDomains"] or "'none'"
    # Nested frames and base URL rewriting are intentionally unsupported. The
    # host advertises no corresponding permissions. Inline scripts are needed by
    # bundled MCP Apps; unsafe-eval, forms and plugin content are never enabled.
    policy = (
        "default-src 'none'; "
        f"script-src 'unsafe-inline' {resources}; style-src 'unsafe-inline' {resources}; "
        f"img-src data: blob: {resources}; font-src data: {resources}; media-src blob: {resources}; "
        f"connect-src {connect}; frame-src 'self'; object-src 'none'; base-uri 'none'; "
        f"form-action 'none'; frame-ancestors {parent}"
    )
    return {
        "Content-Security-Policy": policy,
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), clipboard-read=(), clipboard-write=()"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path != "/sandbox.html" or len(self.path) > 12000:
            self.send_error(404)
            return
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
            parent = origin(query["parent"][0])
            csp = json.loads(query.get("csp", ["{}"])[0])
            headers = sandbox_policy(parent, csp)
            # Same-origin hosting is an invalid deployment, even for a caller
            # bypassing the frontend's equivalent check.
            if urlsplit(parent).netloc.lower() == self.headers.get("Host", "").lower():
                raise ValueError("Sandbox must not share the chat origin")
        except (ValueError, KeyError, TypeError, IndexError):
            self.send_error(400, "Invalid sandbox configuration")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(DOCUMENT)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(DOCUMENT)

    def log_message(self, *_):
        pass  # URLs contain domain policy, not useful application activity.


if __name__ == "__main__":
    # Validate configuration at startup rather than serving a permissive default.
    approved_origins()
    # This listener is the container's sole public service; it has no secrets.
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8092"))), Handler).serve_forever()  # noqa: S104
