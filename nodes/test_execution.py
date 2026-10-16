from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nodes.env_utils import find_settings_module, venv_python
from state import AgentState, TestOutcome, TestResults

TIMEOUT_SECONDS = 300
OUTPUT_LIMIT = 3000

_TEST_RESULT_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<qualname>.+?)\)\s+\.\.\.\s+"
    r"(?P<status>ok|FAIL|ERROR)$",
    re.IGNORECASE,
)
_FAILURE_HEADER_RE = re.compile(
    r"^(?:FAIL|ERROR):\s+.+?\s+\((?P<qualname>[^()]+)\)\s*$"
)


def _tail(raw_output: str, limit: int = OUTPUT_LIMIT) -> str:
    return raw_output[-limit:]


def _as_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _test_type(test_qualname: str) -> str:
    modules = test_qualname.split(".")[:-2]
    if "tests_integration_generated" in modules:
        return "integration"
    if "tests_system_generated" in modules:
        return "system"
    return "unit"


def _failure_tracebacks(raw_output: str) -> dict[str, str]:
    lines = raw_output.splitlines()
    tracebacks: dict[str, str] = {}

    for index, line in enumerate(lines):
        match = _FAILURE_HEADER_RE.match(line.strip())
        if not match:
            continue

        start = index + 1
        while start < len(lines) and not re.fullmatch(r"-{5,}", lines[start].strip()):
            start += 1
        if start < len(lines):
            start += 1

        end = start
        while end < len(lines) and not re.fullmatch(r"[-=]{5,}", lines[end].strip()):
            end += 1

        traceback = "\n".join(lines[start:end]).strip()
        tracebacks[match.group("qualname")] = traceback

    return tracebacks


def _last_exception_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if re.search(r"\b(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Interrupt|Exit)\s*:", stripped):
            return stripped
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _parse_test_output(raw_output: str) -> list[TestOutcome]:
    tracebacks = _failure_tracebacks(raw_output)
    outcomes: list[TestOutcome] = []

    for line in raw_output.splitlines():
        match = _TEST_RESULT_RE.match(line.strip())
        if not match:
            continue

        status = match.group("status").lower()
        qualname = match.group("qualname")
        outcome = "passed" if status == "ok" else ("failed" if status == "fail" else "error")
        traceback = tracebacks.get(qualname, "")
        message = _last_exception_line(traceback) if traceback else line.strip()

        if outcome == "error" and (
            "_FailedTest" in qualname
            or "unittest.loader" in qualname
            or "ImportError" in traceback
            or "ModuleNotFoundError" in traceback
        ):
            message = f"Generated test module failed to load: {message}"
        elif "no such table" in traceback.lower():
            message = f"Database schema is missing, likely because the project has no migrations: {message}"

        outcomes.append(TestOutcome(
            name=qualname,
            test_type=_test_type(qualname),
            outcome=outcome,
            message=message,
            traceback=traceback,
        ))

    return outcomes


def _parse_test_results(raw_output: str, returncode: int) -> TestResults:
    ran_match = re.search(r"\bRan\s+(\d+)\s+tests?\s+in\b", raw_output, re.IGNORECASE)
    if not ran_match:
        output = raw_output or f"Django test command exited with status {returncode} without reporting a test run."
        return TestResults(
            run_status="could_not_run",
            total=0,
            passed=0,
            failed=0,
            errored=0,
            tests=[],
            raw_output=_tail(output),
        )

    tests = _parse_test_output(raw_output)
    total = int(ran_match.group(1))
    failed = 0
    errored = 0

    failed_match = re.search(r"\bFAILED\s*\(([^)]*)\)", raw_output, re.IGNORECASE)
    if failed_match:
        counts = {
            key.lower(): int(value)
            for key, value in re.findall(r"([A-Za-z_]+)\s*=\s*(\d+)", failed_match.group(1))
        }
        failed = counts.get("failures", 0)
        errored = counts.get("errors", 0)
    else:
        failed = sum(test["outcome"] == "failed" for test in tests)
        errored = sum(test["outcome"] == "error" for test in tests)

    return TestResults(
        run_status="ran",
        total=total,
        passed=max(0, total - failed - errored),
        failed=failed,
        errored=errored,
        tests=tests,
        raw_output=_tail(raw_output),
    )


def _write_test_settings(root_dir: Path, settings_module: str) -> str:
    """Create a settings override that makes tests use only in-memory SQLite."""
    settings_package = settings_module.rsplit(".", 1)[0] if "." in settings_module else ""
    settings_dir = root_dir.joinpath(*settings_package.split(".")) if settings_package else root_dir
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "test_settings.py"

    if settings_package:
        original_settings_import = "from .settings import *"
        test_settings_module = f"{settings_package}.test_settings"
    else:
        original_settings_import = "from settings import *"
        test_settings_module = "test_settings"

    settings_file.write_text(
        f"{original_settings_import}\n\n"
        "DATABASES = {\n"
        "    'default': {\n"
        "        'ENGINE': 'django.db.backends.sqlite3',\n"
        "        'NAME': ':memory:',\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return test_settings_module


def _could_not_run(message: str) -> TestResults:
    return TestResults(
        run_status="could_not_run",
        total=0,
        passed=0,
        failed=0,
        errored=0,
        tests=[],
        raw_output=_tail(message),
    )


def test_execution_node(state: AgentState) -> AgentState:
    """Run only generated Django tests against an in-memory SQLite database."""
    if state.get("status") != "ready_for_test_generation":
        return state

    project = state["project"]
    root_dir = Path(project["root_dir"]).resolve()
    test_files = state.get("test_files", {})
    if not test_files:
        return state

    settings_module = find_settings_module(root_dir)
    if settings_module is None:
        return {
            **state,
            "test_results": _could_not_run("Could not locate Django settings.py for the project."),
        }

    manage_py_value = project.get("manage_py_path")
    if not manage_py_value:
        return {
            **state,
            "test_results": _could_not_run("Could not locate manage.py for the project."),
        }

    try:
        test_settings_module = _write_test_settings(root_dir, settings_module)
    except OSError as exc:
        return {
            **state,
            "test_results": _could_not_run(f"Could not write isolated test settings: {exc}"),
        }

    module_labels = sorted({
        relative_path.replace("\\", "/").removesuffix(".py").replace("/", ".")
        for relative_path in test_files
        if relative_path.endswith(".py")
    })
    if not module_labels:
        return state

    command = [
        venv_python(root_dir),
        str(Path(manage_py_value).resolve()),
        "test",
        *module_labels,
        f"--settings={test_settings_module}",
        "-v",
        "2",
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + "\n" + _as_text(exc.stderr)
        message = output or f"Django test run timed out after {TIMEOUT_SECONDS} seconds."
        return {
            **state,
            "test_results": _could_not_run(message),
        }
    except OSError as exc:
        return {
            **state,
            "test_results": _could_not_run(f"Could not start Django test runner: {exc}"),
        }

    raw_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return {
        **state,
        "test_results": _parse_test_results(raw_output, completed.returncode),
    }


test_execution_node.__test__ = False
