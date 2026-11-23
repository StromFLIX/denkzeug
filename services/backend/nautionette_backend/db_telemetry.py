"""Measure SQLite serialization and work without capturing SQL or parameter values."""

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic_ns, time_ns

from opentelemetry import metrics, trace

_tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_buckets = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)
_wait = _meter.create_histogram(
    "denkzeug.sqlite.lock_wait", unit="ms", explicit_bucket_boundaries_advisory=_buckets
)
_work = _meter.create_histogram(
    "denkzeug.sqlite.operation.duration", unit="ms", explicit_bucket_boundaries_advisory=_buckets
)
_slow_ns = 25_000_000


@contextmanager
def observed_lock(lock, operation: str) -> Iterator[None]:
    """Report once the Python lock is released; transaction callers include commit/rollback."""
    wall = time_ns()
    start = monotonic_ns()
    outcome = "ok"
    ended = None
    try:
        with lock:
            entered = monotonic_ns()
            try:
                yield
            finally:
                ended = monotonic_ns()
    except BaseException:
        outcome = "error"
        raise
    finally:
        # threading.Lock enters normally; no measurement if acquisition itself raises.
        if ended is not None:
            labels = {"operation": operation, "outcome": outcome}
            _wait.record((entered - start) / 1_000_000, labels)
            _work.record((ended - entered) / 1_000_000, labels)
            # No span per statement: polling and token persistence are high frequency.
            if (ended - start >= _slow_ns or outcome == "error") and trace.get_current_span().is_recording():
                span = _tracer.start_span(
                    f"sqlite.{operation}",
                    start_time=wall,
                    attributes={
                        "db.system.name": "sqlite",
                        "denkzeug.sqlite.lock_wait_ms": (entered - start) / 1e6,
                    },
                )
                if outcome == "error":
                    span.set_status(trace.StatusCode.ERROR)
                span.end(end_time=wall + ended - start)
