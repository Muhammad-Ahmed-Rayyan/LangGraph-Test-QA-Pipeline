from __future__ import annotations

from pathlib import Path

from graph import MAX_RETRIES, build_graph

__all__ = ["run_testing_qa", "MAX_RETRIES"]


def run_testing_qa(
    project: str | Path,
    *,
    max_retries: int = 0,
    fix_production: bool = False,
    developer_node=None,
) -> dict:
    """Test a Django project and return the result.

    project         a .zip file, or a folder that is already extracted
    max_retries     how many times a failing run may go back to the Developer agent (0 = report once)
    fix_production  also send a not_ready production verdict back to the Developer agent
    developer_node  the Developer agent. Without one, a stub stands in and changes nothing
    """
    path = Path(project)
    if not path.exists():
        raise FileNotFoundError(f"No such file or folder: {path}")
    initial_state = {"zip_path": str(path)} if path.suffix.lower() == ".zip" else {"work_dir": str(path)}

    graph = build_graph(max_retries=max_retries, loop_on_production=fix_production, developer_node=developer_node)
    final = graph.invoke(initial_state)

    report = final.get("report_json") or {}
    project_name = Path(final["project"]["root_dir"]).name if final.get("project") else path.stem
    return {
        "status": final.get("status"),
        "outcome": report.get("outcome"),
        "retry_count": final.get("retry_count", 0),
        "report": report,
        "report_file": str(Path(__file__).resolve().parent / "reports" / f"{project_name}_report.json"),
    }
