from __future__ import annotations

from langgraph.graph import END, StateGraph

from nodes.deployment_artifacts import deployment_artifacts_node
from nodes.developer_stub import demo_fixer_node, developer_feedback_node
from nodes.ingestion import ingestion_node
from nodes.integration_generation import integration_generation_node
from nodes.production_readiness import production_readiness_node
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

    tests_passed      -> production_readiness, then finalize (completed)
    execution_failed  -> finalize (environment_error): the test run itself crashed,
                         which is an environment problem the Developer agent cannot fix
    tests_failed or blocked, retries left -> developer_feedback
    tests_failed or blocked, no retries   -> finalize (needs_human_review)
    """
    outcome = (state.get("report_json") or {}).get("outcome")
    if outcome == "tests_passed":
        return "production_readiness"
    if outcome in ("tests_failed", "blocked") and state.get("retry_count", 0) < max_retries:
        return "developer_feedback"
    return "finalize"


def route_after_production(state: AgentState, max_retries: int = MAX_RETRIES, loop_on_production: bool = False) -> str:
    """Decide whether a project that failed the production checks goes back to the Developer agent.

    By default the production checks are advice only, so the run ends. With loop_on_production
    (only useful when a real Developer agent is connected), a not_ready verdict loops back
    until the retries are used up.
    """
    verdict = (state.get("production_report") or {}).get("verdict")
    if loop_on_production and verdict == "not_ready" and state.get("retry_count", 0) < max_retries:
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


def finalize_node(state: AgentState, loop_on_production: bool = False) -> AgentState:
    """Set the final status once, after the last report."""
    outcome = (state.get("report_json") or {}).get("outcome")
    if outcome == "tests_passed":
        not_ready = (state.get("production_report") or {}).get("verdict") == "not_ready"
        status = "needs_human_review" if loop_on_production and not_ready else "completed"
    elif outcome == "execution_failed":
        status = "environment_error"
    else:
        status = "needs_human_review"
    return {**state, "status": status}


def build_graph(demo_fix: bool = False, max_retries: int = MAX_RETRIES, loop_on_production: bool = False):
    """Build the pipeline. With demo_fix=True a mock fixer replaces the Developer
    agent stub so the full loop (fail, fix, pass) can be shown. Demo only.
    max_retries=0 turns the loop off: the run ends after the first report.
    loop_on_production=True also sends a not_ready production verdict back to the Developer agent."""
    graph = StateGraph(AgentState)

    graph.add_node("ingestion", ingestion_node)
    graph.add_node("runnability_check", runnability_check_node)
    graph.add_node("test_generation", test_generation_node)
    graph.add_node("integration_generation", integration_generation_node)
    graph.add_node("system_generation", system_generation_node)
    graph.add_node("test_execution", test_execution_node)
    graph.add_node("report", report_node)
    graph.add_node("production_readiness", production_readiness_node)
    graph.add_node("deployment_artifacts", deployment_artifacts_node)
    graph.add_node("developer_feedback", demo_fixer_node if demo_fix else developer_feedback_node)
    graph.add_node("finalize", lambda state: finalize_node(state, loop_on_production))

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
        {
            "developer_feedback": "developer_feedback",
            "production_readiness": "production_readiness",
            "finalize": "finalize",
        },
    )
    graph.add_conditional_edges(
        "developer_feedback",
        route_after_developer,
        {"runnability_check": "runnability_check", "test_generation": "test_generation"},
    )
    graph.add_edge("production_readiness", "deployment_artifacts")
    graph.add_conditional_edges(
        "deployment_artifacts",
        lambda state: route_after_production(state, max_retries, loop_on_production),
        {"developer_feedback": "developer_feedback", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph.compile().with_config({"recursion_limit": RECURSION_LIMIT})


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Run the Testing/QA pipeline on a Django project (a .zip or an extracted folder).")
    parser.add_argument("input_path", help="path to a project .zip, or to an already extracted project folder")
    parser.add_argument("--demo-fix", action="store_true",
                        help="DEMO ONLY: a mock fixer stands in for the Developer agent")
    parser.add_argument("--max-retries", type=int, default=None, metavar="N",
                        help="how many times a failing run may loop back to the Developer agent. "
                             f"Default: 0 for a plain run (no real Developer agent is connected, so a retry "
                             f"changes nothing), {MAX_RETRIES} with --demo-fix.")
    parser.add_argument("--fix-production", action="store_true",
                        help="also send a not_ready production verdict back to the Developer agent "
                             "(only useful when a real Developer agent is connected)")
    args = parser.parse_args()

    loop_enabled = args.demo_fix or args.fix_production
    max_retries = args.max_retries if args.max_retries is not None else (MAX_RETRIES if loop_enabled else 0)
    if max_retries < 0:
        parser.error("--max-retries must be 0 or more")

    if args.demo_fix:
        print("[demo-fix] DEMO ONLY: a mock fixer stands in for the Developer agent.", file=sys.stderr)
    app = build_graph(demo_fix=args.demo_fix, max_retries=max_retries, loop_on_production=args.fix_production)

    if args.input_path.endswith(".zip"):
        initial_state = {"zip_path": args.input_path}
    else:
        initial_state = {"work_dir": args.input_path}

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