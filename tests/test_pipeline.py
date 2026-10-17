from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nodes.report as report_module
import nodes.test_execution as execution_module
from graph import build_graph

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path(__file__).parent / "sample_outputs"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_parser_counts_all_passed_tests():
    results = execution_module._parse_test_results(_sample("all_pass.txt"), 0)

    assert results["run_status"] == "ran"
    assert (results["total"], results["passed"], results["failed"], results["errored"]) == (2, 2, 0, 0)
    assert [test["outcome"] for test in results["tests"]] == ["passed", "passed"]
    assert all(test["test_type"] == "unit" for test in results["tests"])


def test_parser_keeps_failure_tracebacks_and_test_types_separate():
    results = execution_module._parse_test_results(_sample("failures.txt"), 1)

    assert results["run_status"] == "ran"
    assert (results["total"], results["passed"], results["failed"], results["errored"]) == (3, 1, 1, 1)
    failed, errored = results["tests"][1:]
    assert failed["outcome"] == "failed"
    assert failed["test_type"] == "unit"
    assert failed["message"] == "AssertionError: 2 != 1"
    assert "tests_generated.py" in failed["traceback"]
    assert errored["outcome"] == "error"
    assert errored["test_type"] == "integration"
    assert "migrations" in errored["message"].lower()
    assert "no such table" in errored["traceback"].lower()
    assert execution_module._test_type(
        "crud_app.tests_system_generated.ProductTests.test_page"
    ) == "system"


def test_parser_classifies_crash_without_ran_line_as_execution_failure():
    results = execution_module._parse_test_results(_sample("crashed.txt"), 1)

    assert results["run_status"] == "could_not_run"
    assert (results["total"], results["passed"], results["failed"], results["errored"]) == (0, 0, 0, 0)
    assert results["tests"] == []
    assert results["raw_output"].endswith("django.db.utils.OperationalError: connection to server failed\n")


@pytest.mark.parametrize(
    ("status", "results", "expected"),
    [
        ("blocked", None, "blocked"),
        ("ready_for_test_generation", {"run_status": "could_not_run"}, "execution_failed"),
        ("ready_for_test_generation", {"run_status": "ran", "failed": 1, "errored": 0}, "tests_failed"),
        ("ready_for_test_generation", {"run_status": "ran", "failed": 0, "errored": 1}, "tests_failed"),
        ("ready_for_test_generation", {"run_status": "ran", "failed": 0, "errored": 0}, "tests_passed"),
    ],
)
def test_report_outcome_decision(status, results, expected):
    assert report_module._report_outcome(status, results) == expected


def test_report_writes_execution_failed_json(tmp_path, monkeypatch):
    report_root = tmp_path / "repo"
    (report_root / "nodes").mkdir(parents=True)
    project_root = tmp_path / "target-project"
    project_root.mkdir()
    monkeypatch.setattr(report_module, "__file__", str(report_root / "nodes" / "report.py"))
    state = {
        "status": "ready_for_test_generation",
        "runnable": True,
        "project": {
            "root_dir": str(project_root),
            "is_django_project": True,
            "django_apps": ["crud_app"],
            "has_docs": True,
        },
        "test_results": {
            "run_status": "could_not_run",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "tests": [],
            "raw_output": _sample("crashed.txt"),
        },
    }

    result = report_module.report_node(state)
    report_path = report_root / "reports" / "target-project_report.json"
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert result["report_json"]["outcome"] == "execution_failed"
    assert saved_report["execution_error"]["error_summary"] == (
        "django.db.utils.OperationalError: connection to server failed"
    )
    assert saved_report["faults"] == []
    assert saved_report["project"]["framework"] == "django"


def test_execution_node_returns_state_unchanged_when_not_ready_or_no_tests():
    pending = {"status": "blocked"}
    empty = {"status": "ready_for_test_generation", "project": {"root_dir": str(REPO_ROOT)}, "test_files": {}}

    assert execution_module.test_execution_node(pending) is pending
    assert execution_module.test_execution_node(empty) is empty


def test_execution_writes_sqlite_override_and_runs_only_generated_modules(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    settings_package = project_root / "myproject"
    settings_package.mkdir(parents=True)
    (settings_package / "settings.py").write_text("DATABASES = {'default': {'ENGINE': 'remote'}}\n", encoding="utf-8")
    manage_py = project_root / "manage.py"
    manage_py.write_text("", encoding="utf-8")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout=_sample("all_pass.txt"), stderr="", returncode=0)

    monkeypatch.setattr(execution_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        execution_module,
        "venv_python",
        lambda root: f"python-from-target-venv:{root.name}",
    )
    state = {
        "status": "ready_for_test_generation",
        "project": {
            "root_dir": str(project_root),
            "manage_py_path": str(manage_py),
            "django_apps": ["crud_app"],
        },
        "test_files": {
            "crud_app/tests_generated.py": "generated",
        },
    }

    result = execution_module.test_execution_node(state)
    test_settings = settings_package / "test_settings.py"
    test_index = captured["command"].index("test")

    assert result["test_results"]["passed"] == 2
    assert captured["command"][test_index + 1] == "crud_app.tests_generated"
    assert captured["command"][test_index + 2] == "--settings=myproject.test_settings"
    assert captured["kwargs"]["cwd"] == str(project_root.resolve())
    assert captured["kwargs"]["timeout"] == execution_module.TIMEOUT_SECONDS
    assert "sqlite3" in test_settings.read_text(encoding="utf-8")
    assert "'NAME': ':memory:'" in test_settings.read_text(encoding="utf-8")


def _run_fixture(zip_name: str) -> dict:
    graph = build_graph()
    return graph.invoke({"zip_path": str(REPO_ROOT / "fixtures" / zip_name)})


def _written_report(project_name: str) -> dict:
    report_path = REPO_ROOT / "reports" / f"{project_name}_report.json"
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.mark.slow
def test_runnable_fixture_passes_and_writes_report():
    result = _run_fixture("runnable_example.zip")

    assert result["test_results"]["run_status"] == "ran"
    assert (result["test_results"]["total"], result["test_results"]["passed"]) == (3, 3)
    assert all(test["outcome"] == "passed" for test in result["test_results"]["tests"])
    assert result["report_json"]["outcome"] == "tests_passed"
    assert _written_report("Django-crud_private-main")["outcome"] == "tests_passed"


@pytest.mark.slow
def test_non_runnable_fixture_is_blocked_and_reported():
    result = _run_fixture("non_runnable_example.zip")

    assert result["report_json"]["outcome"] == "blocked"
    assert result["setup_errors"][0]["stage"] == "django_check"
    assert result.get("test_files", {}) == {}
    assert _written_report("Django-crud_private-main")["outcome"] == "blocked"


@pytest.mark.slow
def test_buggy_fixture_reports_only_the_corrupted_price():
    result = _run_fixture("buggy_example.zip")

    assert result["report_json"]["outcome"] == "tests_failed"
    faults = result["report_json"]["faults"]
    assert len(faults) == 1
    assert faults[0]["test_name"].endswith("test_price_round_trips")
    assert faults[0]["category"] == "assertion_error"
    assert faults[0]["message"] == "AssertionError: 2 != 1"
    assert faults[0]["suspected_location"].endswith("crud_app/models.py")
    assert _written_report("Django-crud_private-main")["outcome"] == "tests_failed"
