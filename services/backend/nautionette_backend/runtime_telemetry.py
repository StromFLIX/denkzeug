"""Backend responsiveness and timing of short operations, not SSE lifetimes."""

import asyncio
from time import monotonic_ns

from nautionette.telemetry import enabled
from opentelemetry import metrics
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_meter = metrics.get_meter(__name__)
_response_start = _meter.create_histogram(
    "denkzeug.backend.http.response_start",
    unit="ms",
    description="Backend HTTP scope start to response headers; not response body or SSE lifetime",
    explicit_bucket_boundaries_advisory=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000),
)
_loop_lag = _meter.create_histogram(
    "denkzeug.backend.event_loop.lag",
    unit="ms",
    description="Delay waking the backend event loop beyond a one-second timer",
    explicit_bucket_boundaries_advisory=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000),
)


class ResponseStartTiming:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not enabled():
            await self.app(scope, receive, send)
            return
        start = monotonic_ns()

        async def timed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                route = getattr(scope.get("route"), "path_format", None) or "unmatched"
                method = scope["method"]
                labels = {
                    "http.route": route,
                    "http.request.method": method if method in _METHODS else "_OTHER",
                    "http.response.status_code": message["status"],
                }
                _response_start.record((monotonic_ns() - start) / 1e6, labels)
            await send(message)

        await self.app(scope, receive, timed_send)


_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


async def observe_event_loop() -> None:
    if not enabled():
        return
    loop = asyncio.get_running_loop()
    while True:
        deadline = loop.time() + 1
        await asyncio.sleep(1)
        _loop_lag.record(max(0, (loop.time() - deadline) * 1000))
