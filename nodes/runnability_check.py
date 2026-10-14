from __future__ import annotations

import subprocess
import venv
from pathlib import Path

from nodes.env_utils import venv_bin_dir, venv_dir_for
from state import AgentState, SetupError

TIMEOUT_SECONDS = 420


def _run(cmd: list[str], cwd: str, timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _make_venv(venv_dir: Path) -> None:
    venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))


def _check_step(cmd: list[str], cwd: str, stage: str, failure_message: str) -> SetupError | None:
    """
    Run one setup command. Returns a SetupError if it fails OR times out,
    otherwise None. A timeout is reported as a normal setup error instead of
    crashing the whole graph.
    """
    try:
        result = _run(cmd, cwd)
    except subprocess.TimeoutExpired:
        return SetupError(
            stage=stage,
            message=f"{failure_message} (timed out after {TIMEOUT_SECONDS}s)",
            raw_output="",
        )
    if result.returncode != 0:
        return SetupError(
            stage=stage,
            message=failure_message,
            raw_output=(result.stdout[-3000:] + "\n" + result.stderr[-3000:]),
        )
    return None


def runnability_check_node(state: AgentState) -> AgentState:
    """
    LangGraph node. Reads state["project"], writes state["runnable"],
    state["setup_errors"], state["status"].
    """
    project = state["project"]
    # Absolute paths: subprocess.run(cwd=...) changes the child's working
    # directory, so a relative executable path would be resolved against it.
    root_dir = Path(project["root_dir"]).resolve()
    setup_errors: list[SetupError] = []

    venv_dir = venv_dir_for(root_dir)
    _make_venv(venv_dir)
    bin_dir = venv_bin_dir(venv_dir)
    pip = str((bin_dir / "pip").resolve())
    python = str((bin_dir / "python").resolve())

    # --- step A: install dependencies ---
    if project["requirements_file"]:
        error = _check_step(
            [pip, "install", "-r", project["requirements_file"]],
            str(root_dir), "dependency_install", "pip install -r requirements.txt failed",
        )
        if error:
            setup_errors.append(error)
    else:
        setup_errors.append(SetupError(
            stage="dependency_install",
            message="No requirements.txt found — cannot verify dependencies",
            raw_output="",
        ))

    # --- step B: Django system check (only if deps installed cleanly and it's a Django project) ---
    if not setup_errors and project["is_django_project"] and project["manage_py_path"]:
        error = _check_step(
            [python, project["manage_py_path"], "check"],
            str(root_dir), "django_check", "manage.py check failed — project does not boot",
        )
        if error:
            setup_errors.append(error)

    runnable = len(setup_errors) == 0
    status = "ready_for_test_generation" if runnable else "blocked"

    return {
        **state,
        "runnable": runnable,
        "setup_errors": setup_errors,
        "status": status,
    }