from pathlib import Path
from textwrap import indent

import pytest
from nautionette_backend.workflow_graph import definition_graph


def test_nested_loops_group_their_body_and_keep_exits_outside():
    graph = definition_graph(
        """
from temporalio import workflow
@workflow.defn
class Demo:
    @workflow.run
    async def run(self, items):
        for item in items:
            if item is None:
                continue
            for part in item:
                if not part:
                    break
                await workflow.execute_activity("fetch", part)
            await workflow.execute_activity("save", item)
        await workflow.execute_activity("finish", {})
""",
        "Demo",
    )
    nodes = graph["nodes"]
    outer, inner = [node for node in nodes if node["kind"] == "loop"]
    by_label = {node["label"]: node for node in nodes}
    assert outer["parent_id"] is None
    assert inner["parent_id"] == outer["id"]
    assert by_label["fetch"]["parent_id"] == inner["id"]
    assert by_label["save"]["parent_id"] == outer["id"]
    assert by_label["finish"]["parent_id"] is None
    next_id = by_label["Next iteration"]["id"]
    assert [edge["target"] for edge in graph["edges"] if edge["source"] == next_id] == [outer["id"]]
    assert any(
        edge["source"] == by_label["Exit loop"]["id"] and edge["target"] == by_label["save"]["id"]
        for edge in graph["edges"]
    )
    assert all(edge["role"] == "repeat" for edge in graph["edges"] if edge["source"] == next_id)


def test_digest_branches_and_early_return():
    code = (Path(__file__).parents[1] / "fixtures/workflow_graph_source.txt").read_text()
    graph = definition_graph(code, "url_digest")
    nodes = graph["nodes"]
    assert [node["label"] for node in nodes if node["kind"] == "activity"] == [
        "http_fetch",
        "agent_call",
        "save_artifact",
    ]
    condition = next(node for node in nodes if node["kind"] == "condition")
    branches = {edge["label"]: edge["target"] for edge in graph["edges"] if edge["source"] == condition["id"]}
    returned = next(node for node in nodes if node["id"] == branches["Yes"])
    assert returned["kind"] == "return"
    assert not any(edge["source"] == returned["id"] for edge in graph["edges"])
    assert next(node for node in nodes if node["id"] == branches["No"])["label"] == "agent_call"


def test_parallel_activities_join_before_next_step():
    graph = definition_graph(
        """
from temporalio import workflow as wf
import asyncio
@wf.defn(name="demo")
class Demo:
    @wf.run
    async def run(self):
        await asyncio.gather(wf.execute_activity("first"), wf.execute_activity("second"))
        await wf.sleep(30)
""",
        "demo",
    )
    nodes = graph["nodes"]
    fork = next(node for node in nodes if node["kind"] == "parallel")
    join = next(node for node in nodes if node["kind"] == "join")
    assert len([edge for edge in graph["edges"] if edge["source"] == fork["id"]]) == 2
    assert len([edge for edge in graph["edges"] if edge["target"] == join["id"]]) == 2
    assert any(node["kind"] == "timer" for node in nodes)


def test_invalid_or_missing_entry_is_an_explicit_partial_graph():
    assert definition_graph("invalid python!", "demo")["warnings"]
    assert definition_graph("print('not a workflow')", "demo")["warnings"]


def test_source_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    graph = definition_graph(f"open({str(marker)!r}, 'w').write('oops')", "demo")
    assert not marker.exists()
    assert graph["warnings"]


def protected_graph(body):
    return definition_graph(
        "from temporalio import workflow\n@workflow.defn\nclass Demo:\n"
        "    @workflow.run\n    async def run(self, items=None):\n" + indent(body, "        "),
        "Demo",
    )


def node_named(graph, label):
    return next(node for node in graph["nodes"] if node["label"] == label)


def connected(graph, source, target, label=None):
    return any(
        edge["source"] == source["id"]
        and edge["target"] == target["id"]
        and (label is None or edge["label"] == label)
        for edge in graph["edges"]
    )


def test_try_expands_activities_handlers_else_and_finally():
    graph = protected_graph("""try:
    await workflow.execute_activity("fetch")
except (ValueError, TypeError) as error:
    await workflow.execute_activity("recover")
except:
    await workflow.execute_activity("fallback")
else:
    await workflow.execute_activity("success")
finally:
    await workflow.execute_activity("cleanup")
await workflow.execute_activity("finish")
""")
    assert [node["label"] for node in graph["nodes"] if node["kind"] == "activity"] == [
        "fetch",
        "success",
        "recover",
        "fallback",
        "cleanup",
        "finish",
    ]
    handlers = [node for node in graph["nodes"] if node["kind"] == "except"]
    assert [node["label"] for node in handlers] == [
        "except (ValueError, TypeError) as error",
        "except Any exception",
    ]
    assert all(node["line"] and node["code"] and node["signature"] for node in handlers)
    assert all(
        edge["role"] == "exception"
        for edge in graph["edges"]
        if edge["target"] in {node["id"] for node in handlers}
    )
    success = node_named(graph, "Else (no exception)")
    assert connected(graph, node_named(graph, "fetch"), success)
    assert not connected(graph, node_named(graph, "recover"), success)
    cleanup = node_named(graph, "Finally (always)")
    for label in ["success", "recover", "fallback"]:
        assert connected(graph, node_named(graph, label), cleanup)
    assert connected(graph, node_named(graph, "cleanup"), node_named(graph, "finish"))
    assert not any(node["label"] == "Unhandled error" for node in graph["nodes"])
    assert not any("single source block" in warning for warning in graph["warnings"])


def test_raise_in_try_reaches_handlers_but_handler_raise_propagates():
    graph = protected_graph("""try:
    raise ValueError("bad")
except ValueError:
    await workflow.execute_activity("recover")
    raise
else:
    await workflow.execute_activity("unreachable_else")
finally:
    await workflow.execute_activity("cleanup")
await workflow.execute_activity("unreachable_finish")
""")
    handler = node_named(graph, "except ValueError")
    raised = [node for node in graph["nodes"] if node["label"] == "Raise error"]
    assert connected(graph, raised[0], handler)
    assert not connected(graph, raised[1], handler)
    assert connected(graph, raised[1], node_named(graph, "Finally (always)"))
    assert not any("unreachable" in node["label"] for node in graph["nodes"])
    terminals = [node for node in graph["nodes"] if node["kind"] == "error"]
    assert terminals
    assert all(not any(edge["source"] == node["id"] for edge in graph["edges"]) for node in terminals)


def test_finally_runs_before_return_and_overrides_pending_exit():
    graph = protected_graph("""try:
    return await workflow.execute_activity("fetch")
finally:
    await workflow.execute_activity("cleanup")
await workflow.execute_activity("unreachable")
""")
    pending = next(
        node for node in graph["nodes"] if node["kind"] == "step" and node["label"] == "Return result"
    )
    assert connected(graph, node_named(graph, "fetch"), pending)
    assert connected(graph, pending, node_named(graph, "Finally (always)"))
    terminal = next(node for node in graph["nodes"] if node["kind"] == "return")
    assert connected(graph, node_named(graph, "Resume after finally"), terminal, "Return result")
    assert not any(node["label"] == "unreachable" for node in graph["nodes"])

    overridden = protected_graph("""try:
    raise ValueError()
finally:
    return "override"
""")
    assert not any(node["kind"] == "error" for node in overridden["nodes"])
    assert not any(node["label"] == "Resume after finally" for node in overridden["nodes"])
    assert len([node for node in overridden["nodes"] if node["kind"] == "return"]) == 1


@pytest.mark.parametrize("transfer, label", [("break", "Exit loop"), ("continue", "Next iteration")])
def test_loop_transfers_run_finally_before_leaving_try(transfer, label):
    graph = protected_graph(f"""for item in items:
    try:
        {transfer}
    finally:
        await workflow.execute_activity("cleanup")
await workflow.execute_activity("finish")
""")
    pending, resumed = [node for node in graph["nodes"] if node["label"] == label]
    assert connected(graph, pending, node_named(graph, "Finally (always)"))
    assert connected(graph, node_named(graph, "Resume after finally"), resumed)
    target = (
        next(node for node in graph["nodes"] if node["kind"] == "loop")
        if transfer == "continue"
        else node_named(graph, "finish")
    )
    assert connected(graph, resumed, target)
    if transfer == "continue":
        assert all(edge["role"] == "repeat" for edge in graph["edges"] if edge["source"] == resumed["id"])


def test_inner_loop_break_does_not_leave_protected_scope():
    graph = protected_graph("""try:
    for item in items:
        break
    await workflow.execute_activity("after_loop")
finally:
    await workflow.execute_activity("cleanup")
""")
    assert connected(graph, node_named(graph, "Exit loop"), node_named(graph, "after_loop"))
    assert not connected(graph, node_named(graph, "Exit loop"), node_named(graph, "Finally (always)"))


def test_nested_finally_cleans_up_from_inner_to_outer():
    graph = protected_graph("""try:
    try:
        return 42
    finally:
        await workflow.execute_activity("inner_cleanup")
finally:
    await workflow.execute_activity("outer_cleanup")
""")
    cleanups = [node for node in graph["nodes"] if node["kind"] == "finally"]
    resumes = [node for node in graph["nodes"] if node["label"] == "Resume after finally"]
    assert len(cleanups) == len(resumes) == 2
    assert connected(graph, node_named(graph, "inner_cleanup"), resumes[0])
    pending = [node for node in graph["nodes"] if node["kind"] == "step" and node["label"] == "Return result"]
    assert connected(graph, resumes[0], pending[1])
    assert connected(graph, pending[1], cleanups[1])
    assert connected(graph, node_named(graph, "outer_cleanup"), resumes[1])


def test_else_raise_is_not_caught_by_the_same_try():
    graph = protected_graph("""try:
    await workflow.execute_activity("fetch")
except ValueError:
    return "handled"
else:
    raise ValueError("else failure")
finally:
    await workflow.execute_activity("cleanup")
""")
    raised = node_named(graph, "Raise error")
    assert not connected(graph, raised, node_named(graph, "except ValueError"))
    assert connected(graph, raised, node_named(graph, "Finally (always)"))
    assert any(node["kind"] == "error" and node["label"] == "Raise error" for node in graph["nodes"])


def test_finally_source_contains_the_entire_block_and_failure_overrides_return():
    graph = protected_graph("""try:
    return "result"
finally:
    await workflow.execute_activity("cleanup")
    raise RuntimeError("cleanup failure")
""")
    cleanup = node_named(graph, "Finally (always)")
    assert 'execute_activity("cleanup")' in cleanup["code"]
    assert 'raise RuntimeError("cleanup failure")' in cleanup["code"]
    assert not any(node["kind"] == "return" for node in graph["nodes"])
    assert any(node["kind"] == "error" for node in graph["nodes"])
    assert graph["warnings"]


def test_except_without_finally_and_exception_groups_are_expanded():
    for keyword in ["except", "except*"]:
        graph = protected_graph(f"""try:
    await workflow.execute_activity("fetch")
{keyword} ValueError as errors:
    await workflow.execute_activity("recover")
await workflow.execute_activity("finish")
""")
        assert connected(graph, node_named(graph, "fetch"), node_named(graph, "finish"))
        assert connected(graph, node_named(graph, "recover"), node_named(graph, "finish"))
        assert any(node["label"] == f"{keyword} ValueError as errors" for node in graph["nodes"])
        assert any(node["kind"] == "error" for node in graph["nodes"])
        if keyword == "except*":
            assert any("Exception-group" in warning for warning in graph["warnings"])


def test_detail_includes_definition_graph(client, backend):
    backend.authoring.add_workflow("demo")
    response = client.get("/api/workflows/demo")
    assert response.status_code == 200
    assert response.json()["graph"]["mode"] == "definition"
