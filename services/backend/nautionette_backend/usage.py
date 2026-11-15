"""Read-only token analytics from saved SQL observations, never model requests.

One chat execution or legacy workflow agent attempt contributes once. Response
usage wins over repeated final-result usage. Only metric metadata is projected;
no job, transcript, tool payload or native session leaves this module.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from .db import db
from .run_statistics import TOKEN_FIELDS, token_counts

GROUPS = {"models", "agents", "workflows", "users", "executions", "runs"}


def window(start: str, end: str, timezone: str) -> tuple[date, date, ZoneInfo, float, float]:
    try:
        first, last, zone = date.fromisoformat(start), date.fromisoformat(end), ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, "Choose valid dates and a supported timezone") from exc
    if not 0 <= (last - first).days < 366:
        raise HTTPException(422, "Choose a date range of at most 366 days")
    lower = datetime.combine(first, time.min, zone).timestamp()
    upper = datetime.combine(last + timedelta(days=1), time.min, zone).timestamp()
    return first, last, zone, lower, upper


def _json(value: Any) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _integer(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _counts(usage: Any, context: Any = None) -> dict | None:
    counts = token_counts(_json(usage))
    if counts is not None:
        return counts
    context = _json(context)
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    if context.get("source") == "provider" and all(type(context.get(k)) is int for k in keys):
        return token_counts(
            {
                "input": context["input_tokens"]
                - context["cache_read_tokens"]
                - context["cache_write_tokens"],
                "output": context["output_tokens"],
                "cacheRead": context["cache_read_tokens"],
                "cacheWrite": context["cache_write_tokens"],
            }
        )
    return None


def _workflow(origin: dict) -> dict:
    if origin.get("kind") == "workflow":
        return origin
    root = origin.get("root")
    return root if isinstance(root, dict) and root.get("kind") == "workflow" else {}


def _facts(lower: float, upper: float, owner: str | None, query) -> list[dict]:
    owner_sql = " AND c.owner_user_id = ?" if owner is not None else ""
    params = (lower, upper, owner) if owner is not None else (lower, upper)
    # Limit the expensive JSON event reads to executions in the selected window.
    # The projection deliberately omits chat titles and full job JSON.
    turns = query(
        "SELECT t.id, t.chat_id, t.state, m.created_at, c.owner_user_id AS user, "
        "COALESCE(u.display_name, u.username, 'Unknown account') AS user_name, "
        "json_extract(t.job, '$.model') AS model, "
        "json_extract(t.job, '$.agent_id') AS agent, "
        "json_extract(t.job, '$.agent_name') AS agent_name, "
        "json_extract(t.job, '$.agent_set') AS agent_set, "
        "COALESCE(json_extract(t.job, '$.origin'), json_extract(m.meta, '$.origin')) AS origin, "
        "json_extract(root.job, '$.origin') AS root_origin "
        "FROM messages m JOIN chat_turns t ON t.user_id = m.id "
        "JOIN chats c ON c.id = t.chat_id LEFT JOIN auth_users u ON u.id = c.owner_user_id "
        "LEFT JOIN agent_calls ac ON ac.child_turn_id = t.id "
        "LEFT JOIN chat_turns root ON root.id = ac.root_turn_id "
        "WHERE m.created_at >= ? AND m.created_at < ? AND t.state NOT IN ('queued','steered')" + owner_sql,
        params,
    )
    facts = {}
    for turn in turns:
        origin = _workflow(_json(turn.pop("origin"))) or _workflow(_json(turn.pop("root_origin")))
        turn.pop("root_origin", None)
        facts[turn["id"]] = {
            **turn,
            "id": f"chat:{turn['id']}",
            "source": "chat",
            "workflow": origin.get("workflow") or "",
            "workflow_id": origin.get("workflow_id") or "",
            "trigger": origin.get("trigger") or "",
            "tokens": None,
            "responses": 0,
            "reports": 0,
            "fallback": None,
            "parts": {},
            "fallback_model": None,
        }
    observations = query(
        "SELECT e.turn_id, json_extract(e.payload, '$.type') AS type, "
        "json_extract(e.payload, '$.usage') AS usage, "
        "json_extract(e.payload, '$.context') AS context "
        "FROM messages m JOIN chat_turns t ON t.user_id = m.id "
        "JOIN chats c ON c.id = t.chat_id JOIN chat_turn_events e ON e.turn_id = t.id "
        "WHERE m.created_at >= ? AND m.created_at < ? AND t.state NOT IN ('queued','steered') "
        "AND json_extract(e.payload, '$.type') IN ('usage','result')" + owner_sql + " ORDER BY e.seq",
        params,
    )
    for event in observations:
        fact = facts.get(event["turn_id"])
        if fact is None:
            continue  # A chat may have been deleted between the bounded reads.
        counts = _counts(event["usage"], event["context"])
        model = _json(event["context"]).get("model") or fact["model"] or ""
        if event["type"] == "usage":
            part = fact["parts"].setdefault(
                model, {"model": model, "tokens": None, "responses": 0, "reports": 0}
            )
            part["responses"] += 1
            if counts is not None:
                part["reports"] += 1
                part["tokens"] = _sum(part["tokens"], counts)
                fact["reports"] += 1
        else:
            # Last terminal result only; a result repeats the last response.
            fact["fallback"] = counts
            fact["fallback_model"] = model
    chat_facts = []
    for fact in facts.values():
        if fact["reports"] == 0 and fact["fallback"]:
            model = fact["fallback_model"]
            part = fact["parts"].setdefault(
                model, {"model": model, "tokens": None, "responses": 0, "reports": 0}
            )
            part["tokens"] = fact["fallback"]
        if not fact["parts"]:
            fact["parts"][fact["model"] or ""] = {
                "model": fact["model"] or "",
                "tokens": None,
                "responses": 0,
                "reports": 0,
            }
        parts = fact.pop("parts")
        fact.pop("fallback")
        fact.pop("fallback_model")
        chat_facts.extend({**fact, **part} for part in parts.values())
    calls = query(
        "SELECT w.call_id, w.workflow_id, w.name AS agent_set, w.model, "
        "CASE WHEN w.status = 'running' AND r.status NOT IN ('running','started') "
        "THEN 'interrupted' ELSE w.status END AS state, "
        "r.created_at, r.workflow, r.trigger, r.owner_user_id AS user, "
        "COALESCE(u.display_name, u.username, 'Unknown account') AS user_name, "
        "json_extract(w.statistics, '$.tokens') AS tokens, "
        "json_extract(w.statistics, '$.model_requests') AS responses, "
        "json_extract(w.statistics, '$.usage_reports') AS reports "
        "FROM runs r JOIN workflow_calls w ON w.workflow_id = r.workflow_id "
        "LEFT JOIN auth_users u ON u.id = r.owner_user_id "
        "WHERE r.created_at >= ? AND r.created_at < ? AND w.kind = 'agent' "
        "AND r.rowid = (SELECT MIN(r2.rowid) FROM runs r2 WHERE r2.workflow_id = r.workflow_id) "
        "AND (? IS NULL OR r.owner_user_id = ?)",
        (lower, upper, owner, owner),
    )
    result = chat_facts
    for call in calls:
        result.append(
            {
                **call,
                "id": f"workflow:{call['workflow_id']}:{call['call_id']}",
                "source": "workflow",
                "chat_id": None,
                "agent": None,
                "agent_name": None,
                "tokens": token_counts(_json(call["tokens"])),
                "responses": _integer(call["responses"]),
                "reports": _integer(call["reports"]),
            }
        )
    return result


def _sum(left: dict | None, right: dict | None) -> dict | None:
    if right is None:
        return left
    return {key: (left or {}).get(key, 0) + right.get(key, 0) for key in TOKEN_FIELDS}


def _metrics(facts: list[dict]) -> dict:
    tokens = None
    for fact in facts:
        tokens = _sum(tokens, fact["tokens"])
    return {
        "tokens": {**tokens, "total": sum(tokens.values())} if tokens is not None else None,
        "executions": len({f["id"] for f in facts}),
        "responses": sum(f["responses"] for f in facts),
        "reports": sum(f["reports"] for f in facts),
    }


def _options(facts: list[dict], key: str, label: str | None = None) -> list[dict]:
    options = {f.get(key) or "": f.get(label or key) or f.get(key) or "Unknown" for f in facts}
    return [
        {"id": k, "label": v}
        for k, v in sorted(options.items(), key=lambda item: (str(item[1]).lower(), item[0]))
    ]


def _row(fact: dict, who) -> dict:
    own = fact["user"] == who.user_id
    return {
        "id": fact["id"],
        "created_at": fact["created_at"],
        "model": fact.get("model") or "Unknown model",
        "agent": fact.get("agent") or "",
        "agent_name": fact.get("agent_name") or "No saved profile",
        "agent_set": fact.get("agent_set") or "",
        "workflow": fact["workflow"],
        "trigger": fact["trigger"],
        "user": fact["user"] or "",
        "user_name": fact["user_name"],
        "state": fact["state"],
        "workload": "workflows" if fact["workflow"] or fact["source"] == "workflow" else "chats",
        "href": (f"/chats/{fact['chat_id']}" if fact["source"] == "chat" else f"/runs/{fact['workflow_id']}")
        if own
        else None,
        **_metrics([fact]),
    }


def report(
    *,
    start: str,
    end: str,
    timezone: str,
    who,
    group: str = "models",
    user: str = "",
    model: str = "",
    agent: str = "",
    workflow: str = "",
    workload: str = "",
    offset: int = 0,
    limit: int = 50,
    sort: str = "tokens",
) -> dict:
    first, last, zone, lower, upper = window(start, end, timezone)
    if (
        group not in GROUPS
        or workload not in {"", "chats", "workflows"}
        or sort not in {"tokens", "newest", "name"}
    ):
        raise HTTPException(422, "Choose a supported breakdown, workload and sort order")
    # Even administrator API keys and delegated agents must not gain workspace
    # analytics through a broad settings grant. Routes require human sessions.
    if who.role != "admin" and user and user != who.user_id:
        raise HTTPException(403, "Usage belongs to your account")
    owner = who.user_id if who.role != "admin" else None
    with db.read_snapshot() as connection:
        facts = _facts(
            lower, upper, owner, lambda sql, params: [dict(row) for row in connection.execute(sql, params)]
        )
    facets = {
        "models": [
            {
                **item,
                "id": item["id"] or "__unknown__",
                "label": item["label"] if item["id"] else "Unknown model",
            }
            for item in _options(facts, "model")
        ],
        "agents": [
            {
                "id": "__none__" if not item["id"] else item["id"],
                "label": "No saved profile" if not item["id"] else item["label"],
            }
            for item in _options(facts, "agent", "agent_name")
        ],
        "workflows": _options([f for f in facts if f["workflow"]], "workflow"),
        "users": [
            {**item, "id": item["id"] or "__unknown__"} for item in _options(facts, "user", "user_name")
        ]
        if who.role == "admin"
        else [],
    }
    selected = [
        f
        for f in facts
        if (not user or (f["user"] or "__unknown__") == user)
        and (not model or (f.get("model") or "__unknown__") == model)
        and (not agent or (f.get("agent") or "__none__") == agent)
        and (not workflow or f["workflow"] == workflow)
        and (
            not workload
            or ("workflows" if f["workflow"] or f["source"] == "workflow" else "chats") == workload
        )
    ]
    if group == "users" and who.role != "admin":
        raise HTTPException(403, "Account breakdown requires an administrator")
    buckets = defaultdict(list)
    for fact in selected:
        day = datetime.fromtimestamp(fact["created_at"], zone).date().isoformat()
        buckets[day].append(fact)
    days = []
    for i in range((last - first).days + 1):
        day = (first + timedelta(days=i)).isoformat()
        daily = buckets[day]
        days.append(
            {
                "date": day,
                **_metrics(daily),
                "chats": _metrics([f for f in daily if not f["workflow"] and f["source"] == "chat"])[
                    "tokens"
                ],
                "workflows": _metrics([f for f in daily if f["workflow"] or f["source"] == "workflow"])[
                    "tokens"
                ],
            }
        )
    rows = []
    if group in {"executions", "runs"}:
        executions = defaultdict(list)
        for fact in selected:
            identity = fact["workflow_id"] if group == "runs" else fact["id"]
            if identity:
                executions[identity].append(fact)
        for items in executions.values():
            row = _row(items[0], who)
            row.update(_metrics(items))
            if any(f["state"] == "running" for f in items):
                row["state"] = "running"
            models = sorted({f.get("model") or "Unknown model" for f in items})
            row["model"] = ", ".join(models)
            row["label"] = row["agent_name"]
            if group == "runs":
                row["id"] = items[0]["workflow_id"]
                row["label"] = items[0]["workflow"] or row["id"]
                row["created_at"] = min(f["created_at"] for f in items)
                row["agent_name"] = ", ".join(
                    sorted({f.get("agent_name") or "No saved profile" for f in items})
                )
                row["href"] = f"/runs/{row['id']}" if all(f["user"] == who.user_id for f in items) else None
            rows.append(row)
    else:
        key = {"models": "model", "agents": "agent", "workflows": "workflow", "users": "user"}[group]
        grouped = defaultdict(list)
        for fact in selected:
            if group == "workflows" and not fact["workflow"]:
                continue
            grouped[fact.get(key) or ""].append(fact)
        for identity, items in grouped.items():
            label = identity
            if group == "agents":
                label = next((f["agent_name"] for f in items if f.get("agent_name")), "No saved profile")
            elif group == "users":
                label = items[0]["user_name"]
            elif group == "models":
                label = identity or "Unknown model"
            rows.append(
                {
                    "id": identity or ("__none__" if group == "agents" else "__unknown__"),
                    "label": label,
                    "created_at": max(f["created_at"] for f in items),
                    **_metrics(items),
                }
            )
    if sort == "newest":
        rows.sort(key=lambda row: (-row["created_at"], row["id"]))
    elif sort == "name":
        rows.sort(key=lambda row: (row["label"].lower(), row["id"]))
    else:
        rows.sort(key=lambda row: (-((row["tokens"] or {}).get("total", -1)), row["id"]))
    return {
        "start": start,
        "end": end,
        "timezone": timezone,
        "admin": who.role == "admin",
        "summary": _metrics(selected),
        "daily": days,
        "facets": facets,
        "group": group,
        "rows": rows[offset : offset + limit],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
    }


def export_csv(data: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = [
        "id",
        "label",
        "created_at",
        "model",
        "agent_name",
        "agent_set",
        "workflow",
        "trigger",
        "user_name",
        "state",
    ]
    writer.writerow(
        ["start", "end", "timezone", *fields, *TOKEN_FIELDS, "total", "executions", "responses", "reports"]
    )
    for row in data["rows"]:
        tokens = row.get("tokens") or {}
        values = [
            data["start"],
            data["end"],
            data["timezone"],
            *[row.get(key, "") for key in fields],
            *[tokens.get(key, "") for key in (*TOKEN_FIELDS, "total")],
            row["executions"],
            row["responses"],
            row["reports"],
        ]
        writer.writerow(
            [
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in values
            ]
        )
    return output.getvalue()
