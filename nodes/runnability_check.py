from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

from state import AgentState, SetupError

TIMEOUT_SECONDS = 420


def _run(cmd: list[str], cwd: str, timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _make_venv(venv_dir: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin")


def runnability_check_node(state: AgentState) -> AgentState:
    project = state["project"]
    root_dir = Path(project["root_dir"]).resolve()
    setup_errors: list[SetupError] = []

    venv_dir = root_dir.parent / f"_venv_{root_dir.name}"
    bin_dir = _make_venv(venv_dir)
    pip = str((bin_dir / "pip").resolve())
    python = str((bin_dir / "python").resolve())

    if project["requirements_file"]:
        result = _run([pip, "install", "-r", project["requirements_file"]], cwd=str(root_dir))
        if result.returncode != 0:
            setup_errors.append(SetupError(
                stage="dependency_install",
                message="pip install -r requirements.txt failed",
                raw_output=(result.stdout[-3000:] + "\n" + result.stderr[-3000:]),
            ))
    else:
        setup_errors.append(SetupError(
            stage="dependency_install",
            message="No requirements.txt found — cannot verify dependencies",
            raw_output="",
        ))

    if not setup_errors and project["is_django_project"] and project["manage_py_path"]:
        result = _run([python, project["manage_py_path"], "check"], cwd=str(root_dir))
        if result.returncode != 0:
            setup_errors.append(SetupError(
                stage="django_check",
                message="manage.py check failed — project does not boot",
                raw_output=(result.stdout[-3000:] + "\n" + result.stderr[-3000:]),
            ))

    runnable = len(setup_errors) == 0
    status = "ready_for_test_generation" if runnable else "blocked"

    return {
        **state,
        "runnable": runnable,
        "setup_errors": setup_errors,
        "status": status,
    }