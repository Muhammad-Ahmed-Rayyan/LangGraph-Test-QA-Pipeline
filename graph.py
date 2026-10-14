from __future__ import annotations

from langgraph.graph import END, StateGraph

from nodes.ingestion import ingestion_node
from nodes.runnability_check import runnability_check_node
from nodes.test_generation import test_generation_node
from state import AgentState


def route_after_runnability_check(state: AgentState) -> str:
    return "blocked_end" if state["status"] == "blocked" else "test_generation"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("ingestion", ingestion_node)
    graph.add_node("runnability_check", runnability_check_node)
    graph.add_node("test_generation", test_generation_node)

    graph.set_entry_point("ingestion")
    graph.add_edge("ingestion", "runnability_check")

    graph.add_node("blocked_end", lambda state: state)

    graph.add_conditional_edges(
        "runnability_check",
        route_after_runnability_check,
        {"test_generation": "test_generation", "blocked_end": "blocked_end"},
    )
    graph.add_edge("test_generation", END)
    graph.add_edge("blocked_end", END)

    return graph.compile()


if __name__ == "__main__":
    import json
    import sys

    input_path = sys.argv[1]
    app = build_graph()

    if input_path.endswith(".zip"):
        initial_state = {"zip_path": input_path}
    else:
        initial_state = {"work_dir": input_path}

    result = app.invoke(initial_state)

    report = {
        "project_root": result["project"]["root_dir"],
        "is_django_project": result["project"]["is_django_project"],
        "django_apps": result["project"]["django_apps"],
        "has_docs": result["project"]["has_docs"],
        "runnable": result["runnable"],
        "status": result["status"],
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
    }
    print(json.dumps(report, indent=2))