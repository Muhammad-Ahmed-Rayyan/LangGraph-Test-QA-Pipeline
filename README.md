# LangGraph Test/QA Pipeline

This pipeline accepts a Django project ZIP, installs its requirements into a
throwaway environment, generates functional unit tests for supported model
fields, runs only those generated tests against an in-memory SQLite database,
and writes a JSON report for the Developer agent.

## Setup

Use Python 3.12 for this project. From the repository root:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

## Run the fixtures

```powershell
python graph.py fixtures\runnable_example.zip
python graph.py fixtures\non_runnable_example.zip
python graph.py fixtures\buggy_example.zip
```

Each run prints the graph state. A machine-readable report is also written to
`reports/<project-name>_report.json`. The runner writes a temporary
`test_settings.py` that overrides the target project's database with
in-memory SQLite; it does not run tests against the project's configured
database.

## Tests

Run the quick pipeline unit tests (no target-project installations) with:

```powershell
pytest -m "not slow"
```

Run the full suite, including the three ZIP-based end-to-end tests, with:

```powershell
pytest
```

The slow tests create throwaway target-project environments and install each
fixture's dependencies, so they can take several minutes on a cold run.