from __future__ import annotations

from langgraph.graph import END, StateGraph

from nodes.developer_stub import demo_fixer_node, developer_feedback_node
from nodes.ingestion import ingestion_node
from nodes.integration_generation import integration_generation_node
from nodes.report import report_node
from nodes.runnability_check import runnability_check_node
from nodes.system_generation import system_generation_node
from nodes.test_execution import test_execution_node
from nodes.test_generation import test_generation_node
from state import AgentState

# How many times a failing run may loop back to the Developer agent. The first
# run is attempt 1, so a project that never gets fixed is run 1 + MAX_RETRIES times.
MAX_RETRIES = 3

# LangGraph stops a run after this many steps (its default is 25). One pass of the
# loop is up to 7 nodes and a run can make 1 + MAX_RETRIES passes, so give it room.
RECURSION_LIMIT = 60


def route_after_runnability_check(state: AgentState) -> str:
    return "report" if state["status"] == "blocked" else "test_generation"


def route_after_report(state: AgentState, max_retries: int = MAX_RETRIES) -> str:
    """Decide whether the run is over or goes back to the Developer agent.

    tests_passed      -> finalize (completed)
    execution_failed  -> finalize (environment_error): the test run itself crashed,
                         which is an environment problem the Developer agent cannot fix
    tests_failed or blocked, retries left -> developer_feedback
    tests_failed or blocked, no retries   -> finalize (needs_human_review)
    """
    outcome = (state.get("report_json") or {}).get("outcome")
    if outcome in ("tests_failed", "blocked") and state.get("retry_count", 0) < max_retries:
        return "developer_feedback"
    return "finalize"


def route_after_developer(state: AgentState) -> str:
    """Where the loop re-enters after the Developer agent has had its turn.

    blocked      -> runnability_check: the problem was dependencies or a boot failure,
                    so the environment has to be checked again. This rebuilds the
                    virtual environment, which is slow.
    tests_failed -> test_generation: the code changed, so the tests are regenerated
                    from the new code. The environment is reused.
    """
    outcome = (state.get("report_json") or {}).get("outcome")
    return "runnability_check" if outcome == "blocked" else "test_generation"


def finalize_node(state: AgentState) -> AgentState:
    """Set the final status once, after the last report."""
    outcome = (state.get("report_json") or {}).get("outcome")
    if outcome == "tests_passed":
        status = "completed"
    elif outcome == "execution_failed":
        status = "environment_error"
    else:
        status = "needs_human_review"
    return {**state, "status": status}


def build_graph(demo_fix: bool = False, max_retries: int = MAX_RETRIES):
    """Build the pipeline. With demo_fix=True a mock fixer replaces the Developer
    agent stub so the full loop (fail, fix, pass) can be shown. Demo only.
    max_retries=0 turns the loop off: the run ends after the first report."""
    graph = StateGraph(AgentState)

    graph.add_node("ingestion", ingestion_node)
    graph.add_node("runnability_check", runnability_check_node)
    graph.add_node("test_generation", test_generation_node)
    graph.add_node("integration_generation", integration_generation_node)
    graph.add_node("system_generation", system_generation_node)
    graph.add_node("test_execution", test_execution_node)
    graph.add_node("report", report_node)
    graph.add_node("developer_feedback", demo_fixer_node if demo_fix else developer_feedback_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("ingestion")
    graph.add_edge("ingestion", "runnability_check")

    graph.add_conditional_edges(
        "runnability_check",
        route_after_runnability_check,
        {"test_generation": "test_generation", "report": "report"},
    )
    graph.add_edge("test_generation", "integration_generation")
    graph.add_edge("integration_generation", "system_generation")
    graph.add_edge("system_generation", "test_execution")
    graph.add_edge("test_execution", "report")

    graph.add_conditional_edges(
        "report",
        lambda state: route_after_report(state, max_retries),
        {"developer_feedback": "developer_feedback", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "developer_feedback",
        route_after_developer,
        {"runnability_check": "runnability_check", "test_generation": "test_generation"},
    )
    graph.add_edge("finalize", END)

    return graph.compile().with_config({"recursion_limit": RECURSION_LIMIT})


if __name__ == "__main__":
    import json
    import sys

    paths = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    demo_fix = "--demo-fix" in sys.argv
    if not paths:
        sys.exit("usage: python graph.py <project.zip | project folder> [--demo-fix]")
    input_path = paths[0]

    if demo_fix:
        print("[demo-fix] DEMO ONLY: a mock fixer stands in for the Developer agent.", file=sys.stderr)
    app = build_graph(demo_fix=demo_fix)

    if input_path.endswith(".zip"):
        initial_state = {"zip_path": input_path}
    else:
        initial_state = {"work_dir": input_path}

    result = app.invoke(initial_state)

    summary = {
        "project_root": result["project"]["root_dir"],
        "is_django_project": result["project"]["is_django_project"],
        "django_apps": result["project"]["django_apps"],
        "has_docs": result["project"]["has_docs"],
        "runnable": result["runnable"],
        "status": result["status"],
        "retry_count": result.get("retry_count", 0),
        "setup_errors": [
            {
                "stage": e["stage"],
                "message": e["message"],
                "raw_output": e["raw_output"][-600:],
            }
            for e in result["setup_errors"]
        ],
        "test_specs": result.get("test_specs", []),
        "generated_test_files": list(result.get("test_files", {}).keys()),
        "test_results": result.get("test_results"),
        "report_json": result.get("report_json"),
    }
    print(json.dumps(summary, indent=2))
