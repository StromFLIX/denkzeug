"""Human input references are declarations, never workflow-provided lookup URLs."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from nautionette import input_problems

from . import access
from .db import db


def resource_fields(schema: Any) -> dict[str, str]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    return {
        key: field["x_resource"]["kind"]
        for key, field in properties.items()
        if isinstance(field, dict)
        and isinstance(field.get("x_resource"), dict)
        and field["x_resource"].get("kind") in {"agent", "chat"}
    }


def _available(kind: str, value: str) -> bool:
    who = access.principal()
    if who is None or who.service:
        return False
    if kind == "chat":
        row = db.one("SELECT owner_user_id FROM chats WHERE id=?", (value,))
        return bool(row and row["owner_user_id"] == who.user_id)
    return bool(db.one("SELECT id FROM agent_profiles WHERE id=?", (value,))) and access.allowed(
        "agent", value
    )


def validate_inputs(name: str, schema: Any, payload: Any, *, scheduled: bool = False) -> None:
    references = resource_fields(schema)
    who = access.principal()
    if references and (who is None or who.service):
        raise HTTPException(403, "Resource input workflows require an accountable user.")
    problems = input_problems(schema, payload)
    fields = []
    if isinstance(schema, dict) and isinstance(payload, dict):
        for error in Draft202012Validator(schema).iter_errors(payload):
            paths = [list(error.path)]
            if error.validator == "required" and isinstance(error.instance, dict):
                paths = [
                    list(error.path) + [key]
                    for key in error.validator_value
                    if key not in error.instance and error.message == f"{key!r} is a required property"
                ]
            for parts in paths:
                path = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)
                fields.append({"path": path, "message": error.message})
    for key, kind in resource_fields(schema).items():
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is not None and (not isinstance(value, str) or not _available(kind, value)):
            message = "This selection is unavailable. Choose another."
            problems.append(f"{key}: {message}")
            fields.append({"path": "/" + key.replace("~", "~0").replace("/", "~1"), "message": message})
    if problems:
        raise HTTPException(400, {"workflow": name, "input": problems, "field_errors": fields})
    if scheduled and resource_fields(schema):
        # Temporal's schedule action bypasses the human start endpoint. Until it
        # carries/revalidates the schedule owner's authority before effects, do
        # not turn a currently valid reference into a permanent unattended grant.
        raise HTTPException(400, "Resource input fields currently support manual runs only.")


def input_options(schema: dict, field: str, query: str, selected: str, offset: int, limit: int) -> dict:
    # A JSON Pointer, not an arbitrary caller-selected resource kind.
    fields = resource_fields(schema)
    key = field[1:].replace("~1", "/").replace("~0", "~") if field.startswith("/") else ""
    kind = fields.get(key)
    if not kind:
        raise HTTPException(400, "This input is not a supported resource field")
    who = access.principal()
    if who is None or who.service:
        raise HTTPException(403, "A user account is required")
    exact = None
    if kind == "chat":
        params: list[Any] = [who.user_id, query.casefold(), query.casefold()]
        rows = db.query(
            "SELECT id,title FROM chats WHERE owner_user_id=? "
            "AND (?='' OR instr(lower(title),?)>0) ORDER BY lower(title),id LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        )
        items = [{"value": row["id"], "label": row["title"] or "Untitled chat"} for row in rows]
        if selected:
            row = db.one("SELECT id,title FROM chats WHERE id=? AND owner_user_id=?", (selected, who.user_id))
            if row:
                exact = {"value": row["id"], "label": row["title"] or "Untitled chat"}
    else:
        rows = db.query(
            "SELECT a.id,a.name,a.description FROM agent_profiles a "
            "WHERE (?='' OR instr(lower(a.name || ' ' || a.description),?)>0) "
            "AND (?='admin' OR EXISTS (SELECT 1 FROM access_grants g WHERE g.kind='agent' "
            "AND g.resource_id=a.id AND g.user_id=?)) "
            "ORDER BY lower(a.name),a.id LIMIT ? OFFSET ?",
            (query.lower(), query.lower(), who.role, who.user_id, limit + 1, offset),
        )
        items = [
            {"value": row["id"], "label": row["name"], "description": row["description"]} for row in rows
        ]
        if selected and _available(kind, selected):
            row = db.one("SELECT id,name,description FROM agent_profiles WHERE id=?", (selected,))
            exact = {"value": row["id"], "label": row["name"], "description": row["description"]}
    return {
        "items": items[:limit],
        "next_offset": offset + limit if len(items) > limit else None,
        "selected": exact,
        "selection_available": exact is not None if selected else None,
    }
