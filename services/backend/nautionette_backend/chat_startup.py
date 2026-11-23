"""Payload-free startup timings; no exporter work is awaited by a turn."""

from time import monotonic_ns, time_ns

from opentelemetry import metrics, trace

_tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000)
_duration = _meter.create_histogram(
    "denkzeug.chat.startup.duration",
    unit="ms",
    description="Backend chat startup stage or time from execution start to the indicated milestone",
    explicit_bucket_boundaries_advisory=_BUCKETS_MS,
)
_turns = _meter.create_counter(
    "denkzeug.chat.startup.turns",
    unit="{turn}",
    description="Chat executions with or without a streamed first text delta",
)

# Each segment's start and end use the same backend monotonic clock. Absent
# milestones (e.g. no reasoning or no native session) don't produce fake zeroes.
_SEGMENTS = (
    ("reconcile", "begin", "reconciled"),
    ("prepare_input", "reconciled", "input_ready"),
    ("prepare_worktrees", "input_ready", "worktrees_ready"),
    ("git_credentials", "worktrees_ready", "git_credentials_ready"),
    ("dispatch_to_broker_started", "agent_dispatch", "started"),
    ("broker_started_to_native_session", "started", "session_ready"),
    ("native_session_to_pi_commands", "session_ready", "commands"),
    ("pi_commands_to_model_phase", "commands", "model_phase"),
    ("model_phase_to_first_text", "model_phase", "first_text"),
    ("time_to_first_reasoning", "begin", "first_reasoning"),
    ("time_to_first_tool", "begin", "tool"),
    ("time_to_first_text", "begin", "first_text"),
)


class ChatStartup:
    def __init__(self, chat_id: str, turn_id: str, job: dict) -> None:
        self._start = monotonic_ns()
        self._wall = time_ns()
        self._marks = {"begin": 0}
        self.milestones: dict[str, float] = {}
        selected = job.get("tools")
        projects = len(job.get("project_ids") or [])
        self._scope = {
            "mcp_selection": "all" if selected is None else "pinned" if selected else "empty",
            "project_count": "multiple" if projects > 1 else "one" if projects == 1 else "none",
        }
        self._span = _tracer.start_span(
            "chat.startup",
            start_time=self._wall,
            attributes={
                "nautionette.chat_id": chat_id,
                "nautionette.turn_id": turn_id,
                "gen_ai.request.model": job.get("model") or "default",
            },
        )

    def mark(self, key: str) -> None:
        if key not in self._marks:
            elapsed = self._marks[key] = monotonic_ns() - self._start
            self.milestones[key] = round(elapsed / 1_000_000, 3)

    def finish(self) -> None:
        if self._span is None:
            return
        span, self._span = self._span, None
        first_text = self._marks.get("first_text")
        end = first_text if first_text is not None else monotonic_ns() - self._start
        parent = trace.set_span_in_context(span)
        for stage, start_key, end_key in _SEGMENTS:
            start, stop = self._marks.get(start_key), self._marks.get(end_key)
            if start is None or stop is None or stop < start:
                continue
            _duration.record((stop - start) / 1_000_000, {**self._scope, "stage": stage}, context=parent)
            if span.is_recording() and stage != "time_to_first_text":
                child = _tracer.start_span(
                    f"chat.startup.{stage}", context=parent, start_time=self._wall + start
                )
                child.end(end_time=self._wall + stop)
        _turns.add(1, {**self._scope, "outcome": "no_text" if first_text is None else "first_text"})
        span.end(end_time=self._wall + end)
