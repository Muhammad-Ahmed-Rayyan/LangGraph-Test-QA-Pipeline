from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import graph as graph_module
import nodes.developer_stub as developer_module
import nodes.report as report_module
import nodes.test_execution as execution_module
import nodes.integration_generation as integration_module
import nodes.system_generation as system_module
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


def _run_fixture(zip_name: str, **options) -> dict:
    """Run the whole pipeline on a fixture. Pass max_retries=0 to look at a single pass."""
    graph = build_graph(**options)
    return graph.invoke({"zip_path": str(REPO_ROOT / "fixtures" / zip_name)})


def _written_report(project_name: str) -> dict:
    report_path = REPO_ROOT / "reports" / f"{project_name}_report.json"
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.mark.slow
def test_runnable_fixture_passes_and_writes_report():
    result = _run_fixture("runnable_example.zip")

    assert result["test_results"]["run_status"] == "ran"
    assert (result["test_results"]["total"], result["test_results"]["passed"]) == (17, 17)
    assert all(test["outcome"] == "passed" for test in result["test_results"]["tests"])
    assert result["report_json"]["outcome"] == "tests_passed"
    by_type = result["report_json"]["summary"]["by_type"]
    # 3 field tests, 4 URLs x (anonymous, logged in), and 6 flows or pages that use stored objects
    assert (by_type["unit"], by_type["integration"], by_type["system"]) == (3, 8, 6)
    assert _written_report("Django-crud_private-main")["outcome"] == "tests_passed"


@pytest.mark.slow
def test_non_runnable_fixture_is_blocked_and_reported():
    result = _run_fixture("non_runnable_example.zip", max_retries=0)

    assert result["report_json"]["outcome"] == "blocked"
    assert result["setup_errors"][0]["stage"] == "django_check"
    assert result.get("test_files", {}) == {}
    assert _written_report("Django-crud_private-main")["outcome"] == "blocked"


@pytest.mark.slow
def test_buggy_fixture_reports_only_the_corrupted_price():
    result = _run_fixture("buggy_example.zip", max_retries=0)

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
        "name": name, "type": field_type, "related": None, "null": False, "blank": False, "primary_key": False,
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
    skipped = {spec["target"]: spec["skipped_reason"] for spec in result["test_specs"]
               if spec["test_type"] == "unit" and "skipped_reason" in spec}
    assert list(skipped) == ["Archive"]


# --- integration test generator (D1) -----------------------------------------


def _url(name, route, **extra):
    base = {
        "name": name, "route": route, "literal_path": None, "params": {}, "supported": True,
        "view_module": "shop.views", "view_name": "view", "view_model": None,
    }
    return {**base, **extra}


def _shop_models():
    return [
        _model("shop.Product", [_field("name", "CharField"), _field("slug", "SlugField")],
               app_name="shop"),
        _model("shop.Order", [_field("note", "TextField")], app_name="shop"),
    ]


def _with_pk(models):
    for model in models:
        model["fields"][0]["primary_key"] = True
        for field in model["fields"][1:]:
            field.setdefault("primary_key", False)
    return models


def test_url_group_is_the_app_that_defines_the_view_and_skips_admin_and_static():
    groups = {"shop"}
    assert integration_module._group_of(_url("home", "", view_module="shop.views"), groups) == "shop"
    assert integration_module._group_of(_url(None, "admin/", view_module="django.contrib.admin.sites"), groups) is None
    assert integration_module._group_of(_url(None, "media/", view_module="django.views.static"), groups) is None


def test_parameter_resolves_through_the_view_model_the_name_or_a_single_model():
    models = _with_pk(_shop_models())
    by_label = {m["label"]: m for m in models}

    from_class = _url("detail", "p/<int:pk>/", params={"pk": "IntConverter"}, view_model="shop.Product")
    from_param = _url("detail", "p/<int:order_id>/", params={"order_id": "IntConverter"})
    from_name = _url("detail", "p/<int:pk>/", params={"pk": "IntConverter"}, view_name="order_detail")

    assert integration_module._resolve_instance(from_class, models, by_label)[0]["model"] == "Product"
    assert integration_module._resolve_instance(from_param, models, by_label)[0]["model"] == "Order"
    assert integration_module._resolve_instance(from_name, models, by_label)[0]["model"] == "Order"
    assert integration_module._resolve_instance(_url("x", "p/<int:pk>/", params={"pk": "IntConverter"}), models, by_label)[0] is None


def test_parameter_that_matches_no_field_or_the_wrong_type_is_skipped_with_a_reason():
    models = _with_pk(_shop_models())
    by_label = {m["label"]: m for m in models}
    year = _url("year", "a/<int:year>/", params={"year": "IntConverter"}, view_model="shop.Product")
    slug_as_int = _url("s", "a/<int:slug>/", params={"slug": "IntConverter"}, view_model="shop.Product")
    by_slug = _url("s", "a/<slug:slug>/", params={"slug": "SlugConverter"}, view_model="shop.Product")

    assert "year" in integration_module._resolve_instance(year, models, by_label)[1]
    assert "SlugField" in integration_module._resolve_instance(slug_as_int, models, by_label)[1]
    assert integration_module._resolve_instance(by_slug, models, by_label)[1] == {"slug": "slug"}


def test_node_adds_integration_tests_without_replacing_the_unit_tests(tmp_path, monkeypatch):
    models = _with_pk(_shop_models()) + [
        _model("auth.User", [_field("username", "CharField"), _field("is_staff", "BooleanField")], app_name="django.contrib.auth"),
    ]
    registry = {"use_tz": True, "models": models, "auth_user_model": "auth.User", "username_field": "username"}
    urls = [
        _url("home", "", literal_path="/"),
        _url("detail", "p/<int:pk>/", params={"pk": "IntConverter"}, view_model="shop.Product"),
        _url("year", "y/<int:year>/", params={"year": "IntConverter"}, view_model="shop.Product"),
        _url(None, "media/<path:p>", params={"p": "PathConverter"}, view_module="django.views.static"),
    ]
    monkeypatch.setattr(integration_module, "_introspect_project", lambda *a: registry)
    monkeypatch.setattr(integration_module, "_introspect_urls", lambda *a: urls)
    monkeypatch.setattr(integration_module, "find_settings_module", lambda root: "config.settings")
    monkeypatch.setattr(integration_module, "venv_python", lambda root: "python")
    (tmp_path / "shop").mkdir()
    unit_spec = {"app": "shop", "test_type": "unit", "target": "Product", "cases": []}

    state = {
        "status": "ready_for_test_generation",
        "project": {"root_dir": str(tmp_path), "django_apps": ["shop"]},
        "test_specs": [unit_spec],
        "test_files": {"shop/tests_generated.py": "# unit"},
    }
    result = integration_module.integration_generation_node(state)

    assert result["test_files"]["shop/tests_generated.py"] == "# unit"
    assert "shop/tests_integration_generated.py" in result["test_files"]
    assert result["test_specs"][0] is unit_spec
    by_target = {spec["target"]: spec for spec in result["test_specs"] if spec["test_type"] == "integration"}
    assert set(by_target) == {"home", "detail", "year"}          # the static URL is dropped silently
    assert "year" in by_target["year"]["skipped_reason"]
    assert [c["name"] for c in by_target["home"]["cases"]] == ["test_home_get_responds", "test_home_get_responds_when_logged_in"]

    content = result["test_files"]["shop/tests_integration_generated.py"]
    compile(content, "tests_integration_generated.py", "exec")
    assert "is_staff=False" in content                           # the login user is not staff
    assert "reverse('detail', kwargs={'pk': obj.pk})" in content
    assert (tmp_path / "shop" / "tests_integration_generated.py").read_text() == content


def test_node_leaves_state_alone_when_the_project_is_not_ready():
    state = {"status": "blocked", "project": {}}
    assert integration_module.integration_generation_node(state) is state


@pytest.mark.slow
def test_field_types_fixture_integration_tests_cover_parameters_namespaces_and_login():
    result = _run_fixture("field_types_example.zip")

    assert result["report_json"]["outcome"] == "tests_passed"
    assert result["report_json"]["summary"]["by_type"]["integration"] == 18
    names = [test["name"] for test in result["test_results"]["tests"]]
    assert any(name.endswith("test_library_book_detail_get_responds") for name in names)
    assert any(name.endswith("test_library_account_get_responds_when_logged_in") for name in names)
    skipped = {spec["target"]: spec["skipped_reason"] for spec in result["test_specs"]
               if spec["test_type"] == "integration" and "skipped_reason" in spec}
    assert list(skipped) == ["library:archive-year"]


# --- system test generator (D2) ----------------------------------------------


def _facts(**extra):
    base = {
        "models_queried": [], "form": None, "templates": {}, "post_keys": [], "file_keys": [],
        "pk_key": None, "pk_model": None, "has_delete": False, "has_save": False,
        "restricted": False, "bases": [], "instance_form": False,
    }
    return {**base, **extra}


def _product_models():
    return _with_pk([_model("shop.Product", [
        _field("name", "CharField"), _field("price", "IntegerField"),
        _field("image", "ImageField", empty_strings_allowed=True),
    ], app_name="shop")])


def test_planner_fills_required_file_fields_only_when_asked_to():
    models = {m["label"]: m for m in _product_models()}

    unit_plan = generation_module._Planner(models, True).plan("shop.Product")
    page_plan = generation_module._Planner(models, True, fill_files=True).plan("shop.Product")

    assert "image" not in dict(unit_plan.args)                 # unit tests leave the upload out
    assert "SimpleUploadedFile" in dict(page_plan.args)["image"]
    assert page_plan.needs_files and page_plan.needs_png


def test_optional_file_fields_stay_empty_even_when_filling_files():
    optional = _model("shop.Doc", [_field("upload", "FileField", blank=True, empty_strings_allowed=True)])

    plan = generation_module._Planner({optional["label"]: optional}, True, fill_files=True).plan("shop.Doc")

    assert plan.ok and "upload" not in dict(plan.args) and not plan.needs_files


def test_marker_goes_on_a_text_field_the_page_prints_and_not_on_one_it_does_not():
    models = {m["label"]: m for m in _product_models()}
    setup = system_module._Setup(generation_module._Planner(models, True, fill_files=True))

    variable, marker = setup.ensure("shop.Product", displayed={"price", "name"})

    assert variable == "rel_shop_product"
    assert marker[0] == "name" and "name='System test product name'" in setup.lines[0].replace('"', "'")
    other = system_module._Setup(generation_module._Planner(models, True, fill_files=True))
    assert other.ensure("shop.Product", displayed={"price"})[1] is None   # price is not a text field


def _shop_state(tmp_path, urls, models=None):
    (tmp_path / "shop").mkdir(exist_ok=True)
    return {
        "status": "ready_for_test_generation",
        "project": {"root_dir": str(tmp_path), "django_apps": ["shop"]},
        "test_specs": [], "test_files": {},
    }, {"use_tz": True, "models": models or _product_models(), "auth_user_model": None, "username_field": None}, urls


def _run_system_node(monkeypatch, tmp_path, urls):
    state, registry, urls = _shop_state(tmp_path, urls)
    monkeypatch.setattr(system_module, "_introspect_project", lambda *a: registry)
    monkeypatch.setattr(system_module, "_introspect_urls", lambda *a: urls)
    monkeypatch.setattr(system_module, "find_settings_module", lambda root: "config.settings")
    monkeypatch.setattr(system_module, "venv_python", lambda root: "python")
    return system_module.system_generation_node(state)


def test_node_builds_page_create_update_and_delete_flows(monkeypatch, tmp_path):
    form = {"model": "shop.Product", "fields": [
        {"name": "name", "kind": "char", "required": True, "related": None, "choice": None},
        {"name": "price", "kind": "integer", "required": True, "related": None, "choice": None},
        {"name": "image", "kind": "image", "required": True, "related": None, "choice": None},
    ]}
    urls = [
        _url("home", "", literal_path="/", facts=_facts(models_queried=["shop.Product"], templates={"index.html": ["name"]})),
        _url("create", "create", literal_path="/create", facts=_facts(form=form, has_save=True)),
        _url("update", "update", literal_path="/update", facts=_facts(
            pk_key="product_id", pk_model="shop.Product", has_save=True, post_keys=["product_id", "name", "price"])),
        _url("delete", "delete", literal_path="/delete", facts=_facts(
            pk_key="product_id", pk_model="shop.Product", has_delete=True, post_keys=["product_id"])),
    ]

    result = _run_system_node(monkeypatch, tmp_path, urls)

    names = [case["name"] for spec in result["test_specs"] for case in spec["cases"]]
    assert names == ["test_home_page_shows_stored_objects", "test_create_flow_creates_object",
                     "test_update_flow_updates_object", "test_delete_flow_deletes_object"]
    content = result["test_files"]["shop/tests_system_generated.py"]
    compile(content, "tests_system_generated.py", "exec")
    assert "@override_settings(MEDIA_ROOT=_MEDIA.name)" in content          # uploads never touch the project's media folder
    assert "'image': SimpleUploadedFile(" in content
    assert "'product_id': rel_shop_product.pk" in content
    assert "assertFalse(Product.objects.filter(pk=pk).exists()" in content
    assert (tmp_path / "shop" / "tests_system_generated.py").read_text() == content


def test_node_skips_views_it_cannot_justify_and_records_why(monkeypatch, tmp_path):
    form = {"model": "shop.Product", "fields": [
        {"name": "name", "kind": "char", "required": True, "related": None, "choice": None},
        {"name": "blob", "kind": None, "required": True, "related": None, "choice": None},
    ]}
    urls = [
        _url("mine", "mine", literal_path="/mine", facts=_facts(models_queried=["shop.Product"], restricted=True)),
        _url("create", "create", literal_path="/create", facts=_facts(form=form, has_save=True)),
        _url("other", "other", literal_path="/other", facts=_facts(
            pk_key="pid", pk_model="shop.Product", has_save=True, post_keys=["pid", "mystery"])),
    ]

    result = _run_system_node(monkeypatch, tmp_path, urls)

    assert "shop/tests_system_generated.py" not in result["test_files"]      # nothing was justified, so no file
    reasons = {spec["target"]: spec["skipped_reason"] for spec in result["test_specs"]}
    assert "restricts" in reasons["page at mine"]
    assert "'blob'" in reasons["create flow at create"]
    assert "'mystery'" in reasons["update flow at other"]


def test_system_node_leaves_state_alone_when_the_project_is_not_ready():
    state = {"status": "blocked", "project": {}}
    assert system_module.system_generation_node(state) is state


@pytest.mark.slow
def test_seeded_flow_bug_is_caught_by_a_system_test_only():
    result = _run_fixture("buggy_flow_example.zip", max_retries=0)

    assert result["report_json"]["outcome"] == "tests_failed"
    faults = result["report_json"]["faults"]
    assert [(f["test_type"], f["test_name"].split(".")[-1]) for f in faults] == [("system", "test_create_flow_creates_object")]
    assert "no Product was saved" in faults[0]["message"]
    by_type = result["report_json"]["summary"]["by_type"]
    assert (by_type["unit"], by_type["integration"]) == (3, 8)              # these still pass: the bug only shows in the flow


@pytest.mark.slow
def test_field_types_fixture_system_flows_cover_forms_choices_edit_and_delete():
    result = _run_fixture("field_types_example.zip")

    assert result["report_json"]["outcome"] == "tests_passed"
    assert result["report_json"]["summary"]["by_type"]["system"] == 10
    names = [test["name"].split(".")[-1] for test in result["test_results"]["tests"]]
    for flow in ("book_create_flow_creates_object", "book_edit_flow_updates_object",
                 "author_create_flow_creates_object", "book_delete_flow_deletes_object"):
        assert f"test_library_{flow}" in names


# --- loop back to the Developer agent (D4) -----------------------------------


@pytest.mark.parametrize("outcome, retry_count, expected", [
    ("tests_passed", 0, "finalize"),
    ("execution_failed", 0, "finalize"),            # an environment problem, not code the Developer agent can fix
    ("tests_failed", 0, "developer_feedback"),
    ("tests_failed", 2, "developer_feedback"),
    ("tests_failed", 3, "finalize"),                # retries used up
    ("blocked", 0, "developer_feedback"),
    ("blocked", 2, "developer_feedback"),
    ("blocked", 3, "finalize"),
])
def test_report_router_loops_back_only_while_retries_remain(outcome, retry_count, expected):
    state = {"report_json": {"outcome": outcome}, "retry_count": retry_count}
    assert graph_module.route_after_report(state) == expected


def test_max_retries_is_three_and_can_be_turned_off():
    assert graph_module.MAX_RETRIES == 3
    state = {"report_json": {"outcome": "tests_failed"}, "retry_count": 0}
    assert graph_module.route_after_report(state, max_retries=0) == "finalize"


def test_loop_re_enters_at_the_stage_that_failed():
    assert graph_module.route_after_developer({"report_json": {"outcome": "blocked"}}) == "runnability_check"
    assert graph_module.route_after_developer({"report_json": {"outcome": "tests_failed"}}) == "test_generation"


@pytest.mark.parametrize("outcome, status", [
    ("tests_passed", "completed"),
    ("execution_failed", "environment_error"),
    ("tests_failed", "needs_human_review"),
    ("blocked", "needs_human_review"),
])
def test_finalize_sets_the_final_status(outcome, status):
    assert graph_module.finalize_node({"report_json": {"outcome": outcome}})["status"] == status


def test_developer_stub_counts_the_retry_writes_the_request_and_leaves_code_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(developer_module, "REPORTS_DIR", tmp_path / "reports")
    project = tmp_path / "my_project"
    project.mkdir()
    (project / "models.py").write_text("x = 1")
    report = {"outcome": "tests_failed", "attempt": 1, "faults": [{"id": "F1"}]}
    state = {
        "project": {"root_dir": str(project)}, "report_json": report, "retry_count": 1,
        "test_results": {"total": 1}, "test_specs": [{"app": "a"}], "test_files": {"a/tests_generated.py": "x"},
    }

    result = developer_module.developer_feedback_node(state)

    assert result["retry_count"] == 2
    assert result["report_json"] == report                       # the router still needs this outcome
    assert (result["test_results"], result["test_specs"], result["test_files"]) == (None, [], {})   # a retry starts clean
    assert json.loads((tmp_path / "reports" / "my_project_developer_request.json").read_text()) == report
    assert (project / "models.py").read_text() == "x = 1"        # the stub never changes code


def test_demo_fixer_undoes_the_seeded_bugs_and_leaves_clean_code_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(developer_module, "REPORTS_DIR", tmp_path / "reports")
    project = tmp_path / "proj"
    (project / "crud_app").mkdir(parents=True)
    models = project / "crud_app" / "models.py"
    views = project / "crud_app" / "views.py"
    models.write_text("class Product:\n    price = 1\n\n    def save(self, *args, **kwargs):\n"
                      "        self.price = self.price * 2\n        super().save(*args, **kwargs)")
    views.write_text("    form.save(commit=False)   # seeded bug\n    return redirect('home')\n")
    state = {"project": {"root_dir": str(project)}, "report_json": {"outcome": "tests_failed"}}

    result = developer_module.demo_fixer_node(state)

    assert "def save" not in models.read_text()
    assert views.read_text() == "    form.save()\n    return redirect('home')\n"
    assert result["retry_count"] == 1

    before = (models.read_text(), views.read_text())
    developer_module.demo_fixer_node(state)                       # nothing left to repair
    assert (models.read_text(), views.read_text()) == before


def _stub_pipeline(monkeypatch, tmp_path, outcome_for_attempt):
    """Replace every node with a counting stub so the loop can be tested in milliseconds."""
    calls = {"runnability": 0, "generation": 0, "report": 0}
    monkeypatch.setattr(developer_module, "REPORTS_DIR", tmp_path / "reports")
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)

    def ingestion(state):
        return {**state, "project": {"root_dir": str(project), "django_apps": []}}

    def runnability(state):
        calls["runnability"] += 1
        outcome = outcome_for_attempt(state.get("retry_count", 0))
        blocked = outcome == "blocked"
        return {**state, "runnable": not blocked, "setup_errors": [],
                "status": "blocked" if blocked else "ready_for_test_generation"}

    def generation(state):
        calls["generation"] += 1
        spec = {"app": "a", "test_type": "unit", "target": "T", "cases": []}
        return {**state, "test_specs": list(state.get("test_specs", [])) + [spec]}

    def report(state):
        calls["report"] += 1
        attempt = state.get("retry_count", 0) + 1
        return {**state, "report_json": {"outcome": outcome_for_attempt(attempt - 1), "attempt": attempt}}

    monkeypatch.setattr(graph_module, "ingestion_node", ingestion)
    monkeypatch.setattr(graph_module, "runnability_check_node", runnability)
    monkeypatch.setattr(graph_module, "test_generation_node", generation)
    monkeypatch.setattr(graph_module, "integration_generation_node", lambda s: s)
    monkeypatch.setattr(graph_module, "system_generation_node", lambda s: s)
    monkeypatch.setattr(graph_module, "test_execution_node", lambda s: s)
    monkeypatch.setattr(graph_module, "report_node", report)
    return calls


def test_failing_tests_stop_after_three_retries_with_needs_human_review(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_failed")

    result = build_graph().invoke({})

    assert result["status"] == "needs_human_review"
    assert result["retry_count"] == 3
    assert calls["report"] == 4                                   # the first run plus 3 retries, then it stops
    assert result["report_json"]["attempt"] == 4
    assert calls["runnability"] == 1                              # tests_failed re-enters at test_generation, not setup
    assert len(result["test_specs"]) == 1                         # each retry starts from empty specs, not appended ones
    assert (tmp_path / "reports" / "proj_developer_request.json").exists()


def test_a_project_that_never_boots_also_stops_after_three_retries(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "blocked")

    result = build_graph().invoke({})

    assert result["status"] == "needs_human_review"
    assert result["retry_count"] == 3
    assert calls["runnability"] == 4                              # blocked re-enters at runnability_check
    assert calls["generation"] == 0


def test_loop_ends_as_completed_once_a_retry_passes(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_failed" if retry_count == 0 else "tests_passed")

    result = build_graph().invoke({})

    assert result["status"] == "completed"
    assert (result["retry_count"], result["report_json"]["attempt"], calls["report"]) == (1, 2, 2)


def test_environment_failure_ends_the_run_without_a_retry(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "execution_failed")

    result = build_graph().invoke({})

    assert result["status"] == "environment_error"
    assert (result.get("retry_count", 0), calls["report"]) == (0, 1)


def test_report_records_the_attempt_number(tmp_path):
    project = tmp_path / "attempt_probe_project"
    project.mkdir()
    state = {"project": {"root_dir": str(project), "django_apps": []}, "status": "ready_for_test_generation",
             "runnable": True, "setup_errors": [], "retry_count": 2}
    report_path = REPO_ROOT / "reports" / "attempt_probe_project_report.json"
    try:
        result = report_module.report_node(state)
    finally:
        report_path.unlink(missing_ok=True)

    assert result["report_json"]["attempt"] == 3


@pytest.mark.slow
def test_buggy_fixture_without_a_fixer_stops_after_three_retries():
    result = _run_fixture("buggy_example.zip")

    assert result["status"] == "needs_human_review"
    assert result["retry_count"] == 3
    assert (result["report_json"]["outcome"], result["report_json"]["attempt"]) == ("tests_failed", 4)
    request = json.loads((REPO_ROOT / "reports" / "Django-crud_private-main_developer_request.json").read_text())
    assert request["outcome"] == "tests_failed" and request["attempt"] == 3


@pytest.mark.slow
@pytest.mark.parametrize("fixture", ["buggy_example.zip", "buggy_flow_example.zip"])
def test_demo_fixer_takes_a_failing_fixture_from_tests_failed_to_tests_passed(fixture):
    result = _run_fixture(fixture, demo_fix=True)

    assert result["status"] == "completed"
    assert (result["retry_count"], result["report_json"]["attempt"]) == (1, 2)
    assert result["report_json"]["outcome"] == "tests_passed"


# --- documentation stays in step with the code (D5) --------------------------


def _mermaid_block(relative_path: str) -> str:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", text, flags=re.S)
    assert len(blocks) == 1, f"{relative_path} should hold exactly one mermaid diagram"
    return blocks[0]


def _diagram_edges(block: str) -> set[tuple[str, str]]:
    names = {"START": "__start__", "END": "__end__"}
    edges = set()
    for line in block.splitlines():
        match = re.match(r"^\s*(\w+)\s*-->(?:\|[^|]*\|)?\s*(\w+)\s*$", line)
        if match:
            edges.add(tuple(names.get(name, name) for name in match.groups()))
    return edges


def test_readme_and_architecture_diagrams_match_the_compiled_graph():
    compiled = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}

    assert _diagram_edges(_mermaid_block("README.md")) == compiled
    assert _diagram_edges(_mermaid_block("docs/architecture.md")) == compiled


def test_readme_and_architecture_show_the_same_diagram():
    assert _mermaid_block("README.md") == _mermaid_block("docs/architecture.md")


def test_architecture_document_describes_every_node():
    text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    nodes = [name for name in build_graph().get_graph().nodes if not name.startswith("__")]

    missing = [name for name in nodes if f"`{name}`" not in text]
    assert not missing


def test_report_example_documents_the_attempt_and_every_outcome():
    example = json.loads((REPO_ROOT / "docs" / "report_example.json").read_text(encoding="utf-8"))

    assert set(example["examples"]) == set(example["outcome_values"])
    for outcome, report in example["examples"].items():
        assert report["outcome"] == outcome
        assert isinstance(report["attempt"], int) and report["attempt"] >= 1
