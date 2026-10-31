from __future__ import annotations

import json
import re
from pathlib import Path

from state import AgentState

_TRACEBACK_FRAME_RE = re.compile(r'^\s*File ["\'](?P<path>.+?)["\'], line \d+', re.MULTILINE)
# A frame plus the source line Python prints under it.
_TRACEBACK_CODE_FRAME_RE = re.compile(
    r'^\s*File ["\'](?P<path>.+?)["\'], line \d+, in [^\n]*\n(?P<code>[^\n]*)', re.MULTILINE
)
_GENERATED_TEST_RE = re.compile(r"(?:^|[/\\])tests(?:_[^/\\]+)?_generated\.py$", re.IGNORECASE)


def _error_summary(raw_output: str) -> str:
    """Return the last traceback line naming an exception, if any."""
    for line in reversed(raw_output.splitlines()):
        stripped = line.strip()
        if re.search(r"\b(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Interrupt|Exit)\s*:", stripped):
            return stripped
    return ""


def _category(outcome: dict) -> str:
    traceback = outcome.get("traceback", "")
    message = outcome.get("message", "")
    exception_text = f"{message}\n{traceback}".lower()
    if "assertionerror" in exception_text:
        return "assertion_error"
    if "importerror" in exception_text or "modulenotfounderror" in exception_text or "_failedtest" in outcome.get("name", ""):
        return "import_error"
    if any(error in exception_text for error in ("integrityerror", "operationalerror", "no such table")):
        return "database_error"
    return "runtime_error"


def _project_relative_location(test_outcome: dict, root_dir: Path) -> str:
    """Select the last project source frame, excluding venv and generated tests."""
    root = root_dir.resolve()
    traceback = test_outcome.get("traceback", "")
    location = ""

    for match in _TRACEBACK_FRAME_RE.finditer(traceback):
        frame_path = Path(match.group("path"))
        try:
            relative = frame_path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part.lower().startswith("_venv_") for part in relative.parts):
            continue
        relative_posix = relative.as_posix()
        if _GENERATED_TEST_RE.search(relative_posix):
            continue
        location = relative_posix

    if location:
        return location

    parts = test_outcome.get("name", "").split(".")
    if len(parts) >= 3:
        if parts[1] == "tests_generated":
            # The assertion runs after save() returns, so its traceback lacks the model frame.
            return f"{parts[0]}/models.py"
        return f"{parts[0]}/{parts[1]}.py"
    return ""


def _fault_confidence(test_outcome: dict, root_dir: Path) -> tuple[str, str]:
    """How sure the report is that a failure is a fault in the project.

    "low" is given only to failures that look like a limit of the generated test:
    the test module did not load, or an ERROR was raised while the generated test
    was building its own objects, with no frame in the project's code. Such a
    failure can still be a real fault (a wrong database constraint, for example),
    so it is labelled and kept, never dropped.
    """
    name = test_outcome.get("name", "")
    message = test_outcome.get("message", "")
    if "_FailedTest" in name or message.startswith("Generated test module failed to load"):
        return "low", "The generated test module failed to load, so this points at the generator more than at the project."
    if test_outcome.get("outcome") != "error":
        return "high", ""

    root = root_dir.resolve()
    project_frame = False
    last_generated_code = ""
    for match in _TRACEBACK_CODE_FRAME_RE.finditer(test_outcome.get("traceback", "")):
        try:
            relative = Path(match.group("path")).resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part.lower().startswith("_venv_") for part in relative.parts):
            continue
        if _GENERATED_TEST_RE.search(relative.as_posix()):
            last_generated_code = match.group("code").strip()
        else:
            project_frame = True

    if not project_frame and ".objects." in last_generated_code:
        return "low", (
            "The error was raised while the generated test was creating its own objects, and no frame "
            "is in the project's code. Check whether the test setup is at fault (a signal or a constraint "
            "the generator did not account for) before changing project code."
        )
    return "high", ""


def _report_outcome(status: str | None, test_results: dict | None) -> str:
    if status == "blocked":
        return "blocked"
    if test_results and test_results.get("run_status") == "could_not_run":
        return "execution_failed"
    if test_results and (test_results.get("failed", 0) + test_results.get("errored", 0)) > 0:
        return "tests_failed"
    return "tests_passed"


def report_node(state: AgentState) -> AgentState:
    """Build the QA report and save it in this repository's reports directory."""
    project = state.get("project") or {}
    project_root_value = project.get("root_dir") or state.get("work_dir")
    if not project_root_value:
        raise ValueError("The report node needs a project root in state['project']['root_dir'].")

    project_root = Path(project_root_value).resolve()
    project_name = project_root.name
    setup_errors = state.get("setup_errors", [])
    test_results = state.get("test_results")
    status = state.get("status")
    runnable = bool(state.get("runnable", status != "blocked"))
    outcome = _report_outcome(status, test_results)

    setup_payload = []
    for error in setup_errors:
        raw_output = error.get("raw_output", "")
        setup_payload.append({
            "stage": error.get("stage", "unknown"),
            "message": error.get("message", ""),
            "error_summary": _error_summary(raw_output),
            "raw_output": raw_output,
        })

    test_outcomes = test_results.get("tests", []) if test_results else []
    by_type = {"unit": 0, "integration": 0, "system": 0}
    for test in test_outcomes:
        test_type = test.get("test_type", "unit")
        if test_type in by_type:
            by_type[test_type] += 1

    total = int(test_results.get("total", 0)) if test_results else 0
    passed = int(test_results.get("passed", 0)) if test_results else 0
    failed = int(test_results.get("failed", 0)) if test_results else 0
    errored = int(test_results.get("errored", 0)) if test_results else 0
    skipped = int(test_results.get("skipped", 0)) if test_results else 0

    faults = []
    if outcome == "tests_failed" and test_results:
        fault_number = 0
        for test in test_outcomes:
            if test.get("outcome") in ("passed", "skipped"):
                continue
            fault_number += 1
            message = test.get("message", "Test failed")
            traceback = test.get("traceback", "")
            if (
                "no such table" in f"{message}\n{traceback}".lower()
                and "migration" not in message.lower()
            ):
                message = f"Database schema is missing, likely because the project has no migrations: {message}"
            confidence, confidence_note = _fault_confidence(test, project_root)
            faults.append({
                "id": f"F{fault_number}",
                "test_name": test.get("name", "unknown"),
                "test_type": test.get("test_type", "unit"),
                "app": test.get("name", "").split(".", 1)[0],
                "category": _category(test),
                "message": message,
                "traceback": traceback,
                "suspected_location": _project_relative_location(test, project_root),
                "confidence": confidence,
                "confidence_note": confidence_note,
            })

    # Tests that chose not to run (for example a form that rejected the generated
    # data). They are not faults, but they are listed so nobody reads "all passed"
    # as "everything was checked".
    skipped_tests = [
        {
            "name": test.get("name", "unknown"),
            "test_type": test.get("test_type", "unit"),
            "reason": test.get("message", ""),
        }
        for test in test_outcomes
        if test.get("outcome") == "skipped"
    ]

    execution_error = None
    if outcome == "execution_failed" and test_results:
        raw_output = test_results.get("raw_output", "")
        execution_error = {
            "message": "Test run crashed before any test executed",
            "error_summary": _error_summary(raw_output),
            "raw_output": raw_output,
        }

    report = {
        "schema_version": "1.1",
        "outcome": outcome,
        "attempt": int(state.get("retry_count", 0)) + 1,
        "project": {
            "name": project_name,
            "framework": "django",
            "apps": project.get("django_apps", []),
            "has_docs": bool(project.get("has_docs", False)),
        },
        "setup": {
            "runnable": runnable,
            "errors": setup_payload,
        },
        "execution_error": execution_error,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "skipped": skipped,
            "by_type": by_type,
        },
        "faults": faults,
        "skipped_tests": skipped_tests,
    }

    repository_root = Path(__file__).resolve().parents[1]
    reports_dir = repository_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{project_name}_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return {
        **state,
        "report_json": report,
    }


report_node.__test__ = False