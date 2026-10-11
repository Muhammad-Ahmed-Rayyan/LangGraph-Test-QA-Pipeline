from __future__ import annotations

from typing import Literal, TypedDict


class SetupError(TypedDict):
    stage: str
    message: str
    raw_output: str


class ProjectProfile(TypedDict):
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


class TestSpec(TypedDict):
    app: str
    test_type: Literal["unit", "integration", "system"]
    target: str
    cases: list[TestCaseSpec]


class AgentState(TypedDict, total=False):
    zip_path: str
    work_dir: str

    project: ProjectProfile

    runnable: bool
    setup_errors: list[SetupError]
    status: Literal["pending", "blocked", "ready_for_test_generation"]

    test_specs: list[TestSpec]
    test_files: dict[str, str]