"""Source-level workflow diagrams, built without importing or executing user code."""

from __future__ import annotations

import ast
from typing import Any

from .activity_details import activity_metadata


def definition_graph(code: str, name: str) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    warnings: list[str] = []
    loops: list[dict[str, Any]] = []
    try_scopes: list[dict[str, Any]] = []

    def add(kind: str, label: str, source: ast.AST | None = None) -> str:
        node_id = f"node-{len(nodes)}"
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "label": label,
                "line": getattr(source, "lineno", None),
                "code": ast.get_source_segment(code, source) if source else None,
                "signature": ast.dump(source) if source else None,
                "parent_id": loops[-1]["id"] if loops else None,
            }
        )
        return node_id

    def connect(incoming: list[tuple[str, str]], target: str, role: str = "flow") -> None:
        for source, label in incoming:
            edges.append(
                {
                    "id": f"edge-{len(edges)}",
                    "source": source,
                    "target": target,
                    "label": label,
                    "role": role,
                }
            )

    root = add("workflow", name)
    graph = {"nodes": nodes, "edges": edges, "warnings": warnings, "mode": "definition"}
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        warnings.append("The workflow source could not be parsed.")
        return graph
    nodes[0]["code"] = code
    nodes[0]["signature"] = ast.dump(tree)

    aliases = {"workflow": "workflow", "asyncio": "asyncio"}
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and item.module in {"temporalio", "temporalio.workflow"}:
            for alias in item.names:
                aliases[alias.asname or alias.name] = (
                    alias.name if item.module == "temporalio" else f"workflow.{alias.name}"
                )
        elif isinstance(item, ast.Import):
            for alias in item.names:
                aliases[alias.asname or alias.name] = alias.name

    def symbol(source: ast.AST) -> str:
        if isinstance(source, ast.Call):
            return symbol(source.func)
        if isinstance(source, ast.Name):
            return aliases.get(source.id, source.id)
        if isinstance(source, ast.Attribute):
            return f"{symbol(source.value)}.{source.attr}".removeprefix("temporalio.")
        return ""

    classes = [item for item in tree.body if isinstance(item, ast.ClassDef)]
    entries = [
        (owner, method)
        for owner in classes
        for method in owner.body
        if isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any(symbol(decorator) == "workflow.run" for decorator in method.decorator_list)
    ]
    matching = [
        entry
        for entry in entries
        if any(
            isinstance(decorator, ast.Call)
            and symbol(decorator) == "workflow.defn"
            and any(
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == name
                for keyword in decorator.keywords
            )
            for decorator in entry[0].decorator_list
        )
        or entry[0].name == name
    ]
    if not matching and len(entries) != 1:
        warnings.append("No unambiguous @workflow.run entry point was found.")
        return graph
    _, entry = (matching or entries)[0]
    nodes[0]["line"] = entry.lineno

    operations = {
        "execute_activity": "activity",
        "execute_local_activity": "activity",
        "start_activity": "activity",
        "start_local_activity": "activity",
        "execute_child_workflow": "child",
        "start_child_workflow": "child",
        "sleep": "timer",
        "wait_condition": "condition",
        "continue_as_new": "continue",
    }

    def expression(source: ast.AST, incoming: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if isinstance(source, ast.Await):
            return expression(source.value, incoming)
        if isinstance(source, ast.Call):
            qualified = symbol(source.func)
            method = qualified.removeprefix("workflow.")
            if qualified in {"asyncio.gather", "workflow.gather"}:
                fork = add("parallel", "In parallel", source)
                connect(incoming, fork)
                ends = []
                for argument in source.args:
                    ends.extend(expression(argument, [(fork, "")]))
                join = add("join", "Wait for all", source)
                connect(ends or [(fork, "")], join)
                return [(join, "")]
            if qualified.startswith("workflow.") and method in operations:
                kind = operations[method]
                argument = (
                    source.args[0]
                    if source.args
                    else next(
                        (
                            keyword.value
                            for keyword in source.keywords
                            if keyword.arg in {"activity", "workflow"}
                        ),
                        None,
                    )
                )
                label = ast.unparse(argument) if argument else method.replace("_", " ")
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    label = argument.value
                if method == "sleep":
                    label = f"Wait {label}"
                elif method == "wait_condition":
                    label = "Wait for condition"
                node_id = add(kind, label, source)
                if kind == "activity":
                    options = {keyword.arg: keyword.value for keyword in source.keywords if keyword.arg}
                    payload = source.args[1] if len(source.args) > 1 else options.get("arg")
                    arguments = options.get("args")
                    if payload is None and isinstance(arguments, (ast.List, ast.Tuple)) and arguments.elts:
                        payload = arguments.elts[0]
                    nodes[-1].update(activity_metadata(label, payload, options=options))
                connect(incoming, node_id)
                if method.startswith("start_"):
                    nodes[-1]["description"] = "Starts asynchronously"
                return [] if kind == "continue" else [(node_id, "")]
        if any(isinstance(item, (ast.Await, ast.Call)) for item in ast.walk(source)):
            node_id = add("step", ast.unparse(source)[:120], source)
            connect(incoming, node_id)
            warnings.append("Helper calls and dynamic expressions are not expanded.")
            return [(node_id, "")]
        return incoming

    def transfer(
        kind: str,
        label: str,
        source: ast.AST,
        incoming: list[tuple[str, str]],
        target: dict[str, Any] | None = None,
        role: str = "flow",
    ) -> None:
        # Only transfers leaving a protected scope must run its cleanup. A break
        # in a loop wholly inside the try must still go straight to that loop's exit.
        scope = next(
            (
                scope
                for scope in reversed(try_scopes)
                if kind == "error"
                or (scope["finally"] and (target is None or any(target is loop for loop in scope["loops"])))
            ),
            None,
        )
        node = add("step" if scope or target else kind, label, source)
        connect(incoming, node, role)
        if scope:
            scope["exits"].append(
                {"kind": kind, "label": label, "source": source, "target": target, "node": node}
            )
        elif kind == "break":
            target["breaks"].append((node, "Exit"))
        elif kind == "continue_loop":
            connect([(node, "Next")], target["id"], role="repeat")

    def block(kind: str, label: str, body: list[ast.stmt]) -> str:
        node = add(kind, label, body[0])
        nodes[-1]["code"] = "\n".join(ast.get_source_segment(code, item) or "" for item in body)
        nodes[-1]["signature"] = ast.dump(ast.Module(body=body, type_ignores=[]))
        return node

    def try_statement(
        statement: ast.Try | ast.TryStar, incoming: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        warnings.append("Exception paths are structural; handler matching depends on runtime errors.")
        if isinstance(statement, ast.TryStar):
            warnings.append("Exception-group handlers may run together; subgroup matching is not expanded.")
        protected = add("try", "Try", statement)
        connect(incoming, protected)
        scope = {"finally": bool(statement.finalbody), "loops": list(loops), "exits": []}
        try_scopes.append(scope)
        normal = statements(statement.body, [(protected, "Try")])
        raised = [exit for exit in scope["exits"] if exit["kind"] == "error"]
        scope["exits"] = [exit for exit in scope["exits"] if exit["kind"] != "error"]
        # Else and handler failures are outside this try's handler matching.
        # They remain pending exits so finally still runs before propagation.
        if statement.orelse and normal:
            success = block("try_else", "Else (no exception)", statement.orelse)
            connect(normal, success)
            normal = statements(statement.orelse, [(success, "")])
        handled = []
        for handler in statement.handlers:
            exception = ast.unparse(handler.type) if handler.type else "Any exception"
            label = f"except{'*' if isinstance(statement, ast.TryStar) else ''} {exception}"
            if handler.name:
                label += f" as {handler.name}"
            catch = add("except", label, handler)
            connect([(protected, "On exception")], catch, role="exception")
            connect([(exit["node"], "Raised") for exit in raised], catch, role="exception")
            handled.extend(statements(handler.body, [(catch, "")]))
        # Typed handlers may not match. A bare except is the only unconditional
        # catch; determining types (or exception-group splits) would execute code.
        if not any(handler.type is None for handler in statement.handlers):
            transfer(
                "error",
                "Unhandled error",
                statement,
                [(protected, "Unhandled")] + [(exit["node"], "Unmatched") for exit in raised],
                role="exception",
            )
        try_scopes.pop()
        normal += handled
        if statement.finalbody:
            cleanup = block("finally", "Finally (always)", statement.finalbody)
            connect([(node, label or "Normal") for node, label in normal], cleanup)
            connect([(exit["node"], exit["label"]) for exit in scope["exits"]], cleanup)
            finished = statements(statement.finalbody, [(cleanup, "")])
            if not finished:
                return []  # A transfer in finally overrides the pending transfer.
            if scope["exits"]:
                resume = add("join", "Resume after finally", statement)
                nodes[-1]["description"] = (
                    "Resumes the incoming path; pending exits do not become normal completion."
                )
                connect(finished, resume)
                finished = [(resume, "Normal")]
            for exit in scope["exits"]:
                transfer(
                    exit["kind"],
                    exit["label"],
                    exit["source"],
                    [(node, exit["label"]) for node, _ in finished],
                    exit["target"],
                )
            normal = finished if normal else []
        else:
            for exit in scope["exits"]:
                transfer(exit["kind"], exit["label"], exit["source"], [(exit["node"], "")], exit["target"])
        return normal

    def statements(body: list[ast.stmt], incoming: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for statement in body:
            if not incoming:
                break
            if isinstance(statement, ast.If):
                decision = add("condition", ast.unparse(statement.test), statement.test)
                connect(incoming, decision)
                incoming = statements(statement.body, [(decision, "Yes")]) + statements(
                    statement.orelse, [(decision, "No")]
                )
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                label = (
                    f"For each {ast.unparse(statement.target)} in {ast.unparse(statement.iter)}"
                    if isinstance(statement, (ast.For, ast.AsyncFor))
                    else f"While {ast.unparse(statement.test)}"
                )
                loop = add("loop", label, statement)
                connect(incoming, loop)
                scope = {"id": loop, "breaks": []}
                loops.append(scope)
                connect(statements(statement.body, [(loop, "Each")]), loop, role="repeat")
                loops.pop()
                incoming = statements(statement.orelse, [(loop, "Done")]) + scope["breaks"]
                warnings.append("Loops are structural; iteration counts depend on the run.")
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                incoming = try_statement(statement, incoming)
            elif isinstance(statement, ast.Return):
                if statement.value:
                    incoming = expression(statement.value, incoming)
                if incoming:
                    transfer("return", "Return result", statement, incoming)
                incoming = []
            elif isinstance(statement, ast.Raise):
                transfer("error", "Raise error", statement, incoming)
                incoming = []
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                incoming = statements(statement.body, incoming)
            elif isinstance(statement, (ast.Break, ast.Continue)) and loops:
                transfer(
                    "continue_loop" if isinstance(statement, ast.Continue) else "break",
                    "Next iteration" if isinstance(statement, ast.Continue) else "Exit loop",
                    statement,
                    incoming,
                    loops[-1],
                )
                incoming = []
            elif isinstance(statement, (ast.Match, ast.Break, ast.Continue)):
                step = add("step", type(statement).__name__, statement)
                connect(incoming, step)
                incoming = [(step, "")]
                warnings.append("Some control flow is shown as a single source block.")
            elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)) and statement.value:
                value = statement.value
                if isinstance(value, ast.Await):
                    incoming = expression(value, incoming)
                elif any(
                    isinstance(item, ast.Call)
                    and symbol(item.func).startswith("workflow.")
                    and symbol(item.func).removeprefix("workflow.") in operations
                    for item in ast.walk(value)
                ):
                    incoming = expression(value, incoming)
        return incoming

    remaining = statements(entry.body, [(root, "")])
    if remaining:
        connect(remaining, add("return", "Complete"))
    graph["warnings"] = list(dict.fromkeys(warnings))
    return graph
