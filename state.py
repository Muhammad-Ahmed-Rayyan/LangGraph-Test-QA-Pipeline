from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class SetupError(TypedDict):
    stage: str
    message: str
    raw_output: str


class ProjectProfile(TypedDict):
    """What the ingestion node learns about the uploaded project."""
    root_dir: str
    has_docs: bool
    doc_files: list[str]
    is_django_project: bool
    manage_py_path: str | None
    django_apps: list[str]
    requirements_file: str | None


class TestCaseSpec(TypedDict):
    name: str
    field: str | None
    checks: str


TestCaseSpec.__test__ = False


class TestSpec(TypedDict):
    """What we plan to test, before any test code is written."""
    app: str
    test_type: Literal["unit", "integration", "system"]
    target: str
    cases: list[TestCaseSpec]
    # Set when the generator could not build a valid object for this model
    # (for example a required field of an unsupported type). The model gets no
    # test class, and the reason is recorded here instead of guessing.
    skipped_reason: NotRequired[str]


TestSpec.__test__ = False


class TestOutcome(TypedDict):
    """Result of one executed test."""
    name: str
    test_type: str
    outcome: Literal["passed", "failed", "error"]
    message: str
    traceback: str


TestOutcome.__test__ = False


class TestResults(TypedDict):
    """Written by the execution node, read by the report node."""
    run_status: Literal["ran", "could_not_run"]
    total: int
    passed: int
    failed: int
    errored: int
    tests: list[TestOutcome]
    raw_output: str


TestResults.__test__ = False

class AgentState(TypedDict, total=False):
    zip_path: str
    work_dir: str

    project: ProjectProfile

    runnable: bool
    setup_errors: list[SetupError]
    status: Literal["pending", "blocked", "ready_for_test_generation"]

    test_specs: list[TestSpec]
    test_files: dict[str, str]

    test_results: TestResults

    report_json: dict