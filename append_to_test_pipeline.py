def _artifact_state(tmp_path, monkeypatch, *, layout="flat", entrypoint=None, requirements="django>=5.0,<6\npsycopg2-binary==2.9.9\n"):
    repo = tmp_path / "repo"
    (repo / "nodes").mkdir(parents=True)
    monkeypatch.setattr(report_module, "__file__", str(repo / "nodes" / "report.py"))
    project = tmp_path / "proj"
    base = project / "site" if layout == "nested" else project
    (base / "config").mkdir(parents=True)
    (base / "manage.py").write_text("")
    (base / "config" / "settings.py").write_text('SECRET_KEY = "a-literal-secret-value-123"\n')
    (base / "config" / "wsgi.py").write_text("")
    (project / "requirements.txt").write_text(requirements, encoding="utf-8")
    if entrypoint is None:
        dotted = "site.config.wsgi" if layout == "nested" else "config.wsgi"
        entrypoint = {"wsgi": dotted, "asgi": None}
    production = {
        "verdict": "not_ready",
        "findings": [
            {"id": "P1", "severity": "high", "category": "secrets",
             "message": "SECRET_KEY is a literal string in the source (config/settings.py, line 1).", "location": "config/settings.py"},
            {"id": "P2", "severity": "low", "category": "settings", "message": "Database 'default' is SQLite.", "location": "config.settings"},
        ],
        "entrypoint": entrypoint, "artifacts": [],
    }
    state = {
        "project": {"root_dir": str(project), "manage_py_path": str(base / "manage.py"), "is_django_project": True,
                    "requirements_file": str(project / "requirements.txt")},
        "report_json": {"outcome": "tests_passed", "production": production},
    }
    return state, repo


def _deployment_files(repo: Path) -> dict[str, str]:
    folder = repo / "reports" / "proj_deployment"
    return {path.name: path.read_text(encoding="utf-8") for path in folder.iterdir()}


def test_artifacts_are_built_from_the_projects_own_files_and_never_hold_secret_values(tmp_path, monkeypatch):
    state, repo = _artifact_state(tmp_path, monkeypatch)

    result = artifacts_module.deployment_artifacts_node(state)

    files = _deployment_files(repo)
    assert set(files) == {"Dockerfile", ".dockerignore", "docker-compose.yml", ".env.example", "DEPLOYMENT.md"}
    assert 'CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]' in files["Dockerfile"]
    assert "RUN pip install --no-cache-dir gunicorn" in files["Dockerfile"]            # not in requirements, so it is added
    assert "COPY requirements.txt /app/requirements.txt" in files["Dockerfile"]
    assert "  db:" in files["docker-compose.yml"] and "service_healthy" in files["docker-compose.yml"]   # a Postgres driver is listed
    assert "**/tests_generated.py" in files[".dockerignore"] and "**/test_settings.py" in files[".dockerignore"]
    assert "P1" in files["DEPLOYMENT.md"] and "The settings do not read the environment" in files["DEPLOYMENT.md"]
    assert "a-literal-secret-value" not in "".join(files.values())
    production = result["report_json"]["production"]
    assert [item["file"] for item in production["artifacts"]][0] == "Dockerfile"
    assert all(check["ok"] for check in production["artifact_checks"])
    assert result["production_report"] is production
    saved = json.loads((repo / "reports" / "proj_report.json").read_text(encoding="utf-8"))
    assert saved["production"]["artifacts"] and saved["outcome"] == "tests_passed"


def test_a_manage_py_in_a_subfolder_sets_the_working_directory_and_the_module_path(tmp_path, monkeypatch):
    state, repo = _artifact_state(tmp_path, monkeypatch, layout="nested")

    artifacts_module.deployment_artifacts_node(state)

    dockerfile = _deployment_files(repo)["Dockerfile"]
    assert "WORKDIR /app/site" in dockerfile
    assert '"config.wsgi:application"' in dockerfile and "site.config.wsgi" not in dockerfile


def test_an_asgi_only_project_gets_uvicorn_and_a_listed_server_is_not_installed_again(tmp_path, monkeypatch):
    state, repo = _artifact_state(tmp_path, monkeypatch, entrypoint={"wsgi": None, "asgi": "config.asgi"},
                                  requirements="django>=5.0,<6\nuvicorn==0.30.0\n")
    (tmp_path / "proj" / "config" / "asgi.py").write_text("")

    artifacts_module.deployment_artifacts_node(state)

    files = _deployment_files(repo)
    assert 'CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]' in files["Dockerfile"]
    assert "pip install --no-cache-dir uvicorn" not in files["Dockerfile"]
    assert "  db:" not in files["docker-compose.yml"]                                      # no Postgres driver listed


def test_without_an_entry_point_only_the_checklist_is_written_and_the_failed_check_is_recorded(tmp_path, monkeypatch):
    state, repo = _artifact_state(tmp_path, monkeypatch, entrypoint={"wsgi": None, "asgi": None})

    result = artifacts_module.deployment_artifacts_node(state)

    assert set(_deployment_files(repo)) == {"DEPLOYMENT.md"}
    checks = result["report_json"]["production"]["artifact_checks"]
    assert any(not check["ok"] and "entry point" in check["check"] for check in checks)


def test_the_artifact_node_leaves_state_alone_when_there_is_no_production_block():
    state = {"project": {"is_django_project": True, "manage_py_path": "x"}, "report_json": {"outcome": "tests_passed"}}
    assert artifacts_module.deployment_artifacts_node(state) is state

def _view_crash(project: Path, test_name: str, test_type: str, line: int = 9) -> dict:
    trace = (
        "Traceback (most recent call last):\n"
        f'  File "{project / "app" / "tests_integration_generated.py"}", line 21, in {test_name}\n    response = self.client.get(url)\n'
        f'  File "{project.parent / "_venv_proj" / "Lib" / "site-packages" / "django" / "test" / "client.py"}", line 743, in check_exception\n    raise exc_value\n'
        f'  File "{project / "app" / "views.py"}", line {line}, in index\n    raise Exception("seeded view bug")\n'
        "Exception: seeded view bug"
    )
    return {"name": f"app.tests_x.T.{test_name}", "test_type": test_type, "outcome": "error",
            "message": "Exception: seeded view bug", "traceback": trace}


def test_tests_that_crash_on_the_same_project_line_become_one_fault(tmp_path, monkeypatch):
    state, project = _state_for(tmp_path, monkeypatch, [], failed=0, errored=5, total=5)
    state["test_results"]["tests"] = [
        _view_crash(project, "test_home", "integration"),
        _view_crash(project, "test_home_logged_in", "integration"),
        _view_crash(project, "test_home_page_shows_objects", "system"),
        _view_crash(project, "test_other_view", "integration", line=20),            # a different line is a different fault
        {"name": "app.tests_generated.M.test_setup", "test_type": "unit", "outcome": "error",
         "message": "Exception: seeded view bug", "traceback": "Exception: seeded view bug"},  # no project frame: never merged
    ]

    faults = report_module.report_node(state)["report_json"]["faults"]

    assert [fault["id"] for fault in faults] == ["F1", "F2", "F3"]
    assert len(faults[0]["affected_tests"]) == 3 and faults[0]["suspected_location"] == "app/views.py"
    assert faults[1]["affected_tests"] == [] and faults[2]["affected_tests"] == []
    assert faults[0]["test_name"].endswith("test_home")


def test_a_single_fault_is_left_alone_by_the_grouping(tmp_path, monkeypatch):
    state, project = _state_for(tmp_path, monkeypatch, [], failed=1, errored=0, total=1)
    state["test_results"]["tests"] = [{
        "name": "app.tests_generated.M.test_price_round_trips", "test_type": "unit", "outcome": "failed",
        "message": "AssertionError: 2 != 1",
        "traceback": f'Traceback (most recent call last):\n  File "{project / "app" / "tests_generated.py"}", line 17, in t\n    self.assertEqual(obj.price, 1)\nAssertionError: 2 != 1',
    }]

    fault = report_module.report_node(state)["report_json"]["faults"][0]

    assert fault["id"] == "F1" and fault["affected_tests"] == []

@pytest.mark.parametrize("verdict, retry_count, loop, expected", [
    ("not_ready", 0, True, "developer_feedback"),
    ("not_ready", 3, True, "finalize"),                  # retries used up
    ("not_ready", 0, False, "finalize"),                 # the production checks are advice unless asked otherwise
    ("ready_with_warnings", 0, True, "finalize"),
    ("ready", 0, True, "finalize"),
])
def test_production_router_loops_back_only_when_asked_and_only_for_not_ready(verdict, retry_count, loop, expected):
    state = {"production_report": {"verdict": verdict}, "retry_count": retry_count}
    assert graph_module.route_after_production(state, max_retries=3, loop_on_production=loop) == expected


def test_a_not_ready_verdict_changes_the_final_status_only_when_the_production_loop_is_on():
    state = {"report_json": {"outcome": "tests_passed"}, "production_report": {"verdict": "not_ready"}}

    assert graph_module.finalize_node(state)["status"] == "completed"
    assert graph_module.finalize_node(state, loop_on_production=True)["status"] == "needs_human_review"


def _production_stub(verdict_for_retry):
    def production(state):
        return {**state, "production_report": {"verdict": verdict_for_retry(state.get("retry_count", 0))}}
    return production


def test_the_production_loop_runs_until_the_verdict_is_ready_and_then_completes(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_passed")
    monkeypatch.setattr(graph_module, "production_readiness_node",
                        _production_stub(lambda retry_count: "not_ready" if retry_count < 2 else "ready"))

    result = build_graph(loop_on_production=True).invoke({})

    assert (result["status"], result["retry_count"], calls["report"]) == ("completed", 2, 3)
    assert calls["runnability"] == 1                              # the code changed, not the environment


def test_the_production_loop_stops_at_the_retry_limit(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_passed")
    monkeypatch.setattr(graph_module, "production_readiness_node", _production_stub(lambda retry_count: "not_ready"))

    result = build_graph(loop_on_production=True).invoke({})

    assert (result["status"], result["retry_count"], calls["report"]) == ("needs_human_review", 3, 4)


def test_without_the_production_flag_a_not_ready_verdict_does_not_loop(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_passed")
    monkeypatch.setattr(graph_module, "production_readiness_node", _production_stub(lambda retry_count: "not_ready"))

    result = build_graph().invoke({})

    assert (result["status"], result.get("retry_count", 0), calls["report"]) == ("completed", 0, 1)

def test_a_real_developer_agent_can_be_plugged_into_the_loop(monkeypatch, tmp_path):
    calls = _stub_pipeline(monkeypatch, tmp_path, lambda retry_count: "tests_failed" if retry_count == 0 else "tests_passed")
    seen = []

    def my_developer(state):
        seen.append(state["report_json"]["outcome"])                  # it reads the report of the attempt that just ended
        return developer_module._next_attempt(state)                  # and returns the state the loop expects

    result = build_graph(max_retries=3, developer_node=my_developer).invoke({})

    assert seen == ["tests_failed"]
    assert (result["status"], result["retry_count"], calls["report"]) == ("completed", 1, 2)


def test_run_testing_qa_accepts_a_zip_or_a_folder_and_returns_the_report(monkeypatch, tmp_path):
    captured = {}

    class FakeGraph:
        def invoke(self, initial_state):
            captured["state"] = initial_state
            return {"status": "completed", "retry_count": 0, "project": {"root_dir": str(tmp_path / "my_project")},
                    "report_json": {"outcome": "tests_passed", "faults": []}}

    def fake_build_graph(**options):
        captured["options"] = options
        return FakeGraph()

    monkeypatch.setattr(testing_qa, "build_graph", fake_build_graph)
    archive = tmp_path / "project.zip"
    archive.write_bytes(b"")
    folder = tmp_path / "extracted"
    folder.mkdir()

    from_zip = testing_qa.run_testing_qa(archive, max_retries=2)
    assert captured["state"] == {"zip_path": str(archive)}
    assert captured["options"] == {"max_retries": 2, "loop_on_production": False, "developer_node": None}
    assert (from_zip["status"], from_zip["outcome"], from_zip["retry_count"]) == ("completed", "tests_passed", 0)
    assert from_zip["report"] == {"outcome": "tests_passed", "faults": []}
    assert from_zip["report_file"].endswith("my_project_report.json")

    testing_qa.run_testing_qa(folder, fix_production=True)
    assert captured["state"] == {"work_dir": str(folder)}
    assert captured["options"]["loop_on_production"] is True


def test_run_testing_qa_rejects_a_path_that_does_not_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        testing_qa.run_testing_qa(tmp_path / "missing.zip")