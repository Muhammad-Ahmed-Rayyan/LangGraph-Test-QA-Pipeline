from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nodes.report as report_module
import nodes.test_execution as execution_module
import nodes.test_generation as generation_module
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


# --- unit test generator: field types and relations (D3) ---------------------


def _field(name, field_type, **extra):
    base = {
        "name": name, "type": field_type, "related": None, "null": False,
        "has_default": False, "empty_strings_allowed": field_type in {"CharField", "TextField", "FileField"},
        "auto_created": False, "auto_now": False, "max_digits": None, "decimal_places": None,
    }
    return {**base, **extra}


def _model(label, fields, **extra):
    app_label, name = label.split(".")
    base = {
        "label": label, "model": name, "app_label": app_label, "app_name": app_label,
        "managed": True, "proxy": False,
        "fields": [_field("id", "BigAutoField", auto_created=True), *fields],
    }
    return {**base, **extra}


def _planner(models, use_tz=True):
    return generation_module._Planner({m["label"]: m for m in models}, use_tz)


def test_every_requested_field_type_has_a_sample_value():
    wanted = {
        "CharField", "TextField", "BooleanField", "IntegerField", "FloatField", "EmailField",
        "DateField", "DateTimeField", "DecimalField", "URLField", "SlugField",
        "PositiveIntegerField", "BigIntegerField", "UUIDField",
    }
    assert wanted <= set(generation_module._FIELD_SAMPLE_VALUES)


def test_datetime_and_decimal_samples_follow_project_settings():
    datetime_field = _field("when", "DateTimeField")
    assert "tzinfo" in generation_module._sample_expr(datetime_field, use_tz=True)
    assert "tzinfo" not in generation_module._sample_expr(datetime_field, use_tz=False)

    fits = _field("price", "DecimalField", max_digits=6, decimal_places=2)
    only_fraction = _field("rate", "DecimalField", max_digits=3, decimal_places=3)
    assert generation_module._sample_expr(fits, True) == 'decimal.Decimal("1")'
    assert generation_module._sample_expr(only_fraction, True) == 'decimal.Decimal("0.1")'


def test_planner_creates_related_objects_first_and_reuses_shared_ones():
    publisher = _model("shop.Publisher", [_field("name", "CharField")])
    author = _model("shop.Author", [_field("publisher", "ForeignKey", related="shop.Publisher")])
    book = _model("shop.Book", [
        _field("author", "ForeignKey", related="shop.Author"),
        _field("publisher", "ForeignKey", related="shop.Publisher"),
    ])

    plan = _planner([publisher, author, book]).plan("shop.Book")

    assert plan.ok
    created = [line.split(" = ")[0] for line in plan.setup_lines]
    assert created == ["rel_shop_publisher", "rel_shop_author"]   # one Publisher shared by both relations
    assert dict(plan.args) == {"author": "rel_shop_author", "publisher": "rel_shop_publisher"}


def test_planner_omits_optional_self_reference_but_skips_required_one():
    optional = _model("shop.Category", [_field("parent", "ForeignKey", related="shop.Category", null=True)])
    required = _model("shop.Node", [_field("parent", "ForeignKey", related="shop.Node")])

    assert _planner([optional]).plan("shop.Category").args == []
    skipped = _planner([required]).plan("shop.Node")
    assert not skipped.ok
    assert "cycle" in skipped.reason


def test_planner_stops_at_the_depth_limit():
    chain = [_model(f"shop.M{i}", [_field("next", "ForeignKey", related=f"shop.M{i + 1}")]) for i in range(6)]
    chain.append(_model("shop.M6", []))

    plan = _planner(chain).plan("shop.M0")

    assert not plan.ok
    assert "deep" in plan.reason


def test_planner_omits_file_fields_and_skips_unfillable_required_fields():
    with_file = _model("shop.Doc", [_field("title", "CharField"), _field("upload", "FileField")])
    with_json = _model("shop.Blob", [_field("payload", "JSONField")])
    optional_json = _model("shop.Opt", [_field("payload", "JSONField", null=True)])
    unmanaged = _model("shop.Legacy", [], managed=False)

    assert [name for name, _ in _planner([with_file]).plan("shop.Doc").args] == ["title"]
    assert "JSONField" in _planner([with_json]).plan("shop.Blob").reason
    assert _planner([optional_json]).plan("shop.Opt").ok
    assert not _planner([unmanaged]).plan("shop.Legacy").ok


def test_rendered_test_file_is_valid_python_and_names_foreign_models_without_clashes():
    user = _model("auth.User", [_field("username", "CharField")], app_name="django.contrib.auth")
    other_review = _model("other.Review", [_field("text", "TextField")], app_name="other")
    review = _model("shop.Review", [
        _field("reviewer", "ForeignKey", related="auth.User"),
        _field("echo", "ForeignKey", related="other.Review"),
        _field("posted", "DateField"),
    ])
    planner = _planner([user, other_review, review])
    plan = planner.plan("shop.Review")
    spec = generation_module._build_spec("shop", review, plan)

    content = generation_module._render_app_test_file([(spec, review, plan)], planner.models)

    compile(content, "tests_generated.py", "exec")
    assert "from django.contrib.auth.models import User" in content
    assert "from other.models import Review as Review_other" in content
    assert "import datetime" in content
    assert "<<" not in content


def test_skipped_model_records_why_and_gets_no_cases():
    blob = _model("shop.Blob", [_field("payload", "JSONField")])
    plan = _planner([blob]).plan("shop.Blob")

    spec = generation_module._build_spec("shop", blob, plan)

    assert spec["cases"] == []
    assert "JSONField" in spec["skipped_reason"]


@pytest.mark.slow
def test_field_types_fixture_generates_passing_tests_for_relations_and_dates():
    result = _run_fixture("field_types_example.zip")

    assert result["report_json"]["outcome"] == "tests_passed"
    summary = result["report_json"]["summary"]
    assert summary["total"] > 0 and summary["passed"] == summary["total"]
    names = [test["name"] for test in result["test_results"]["tests"]]
    assert any(name.endswith("BookModelTests.test_author_relation_round_trips") for name in names)
    assert any(name.endswith("BookModelTests.test_published_at_round_trips") for name in names)
    skipped = {spec["target"]: spec["skipped_reason"] for spec in result["test_specs"] if "skipped_reason" in spec}
    assert list(skipped) == ["Archive"]
