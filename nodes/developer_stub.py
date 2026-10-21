from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from state import AgentState

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"

def _request_path(state: AgentState) -> Path:
    project_name = Path(state["project"]["root_dir"]).resolve().name
    return REPORTS_DIR / f"{project_name}_developer_request.json"


def _write_request(state: AgentState) -> Path:
    """Hand the report to the Developer agent. This stub only writes the file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _request_path(state)
    path.write_text(json.dumps(state.get("report_json") or {}, indent=2), encoding="utf-8")
    return path


def _next_attempt(state: AgentState) -> AgentState:
    """Count the retry and empty what the next pass will rebuild.

    test_generation, integration_generation and system_generation each append to
    test_specs and test_files, so stale values would duplicate specs and could point
    the executor at tests from the old attempt. The values are set to empty on
    purpose: LangGraph only updates the keys a node returns, so leaving a key out
    would keep the old value. report_json stays, because the router reads the
    outcome of the attempt that just ended.
    """
    return {
        **state,
        "test_results": None,
        "test_specs": [],
        "test_files": {},
        "retry_count": state.get("retry_count", 0) + 1,
    }


def developer_feedback_node(state: AgentState) -> AgentState:
    """
    Stand-in for the real Developer agent, which another group is building.

    Counts the retry and writes reports/<project name>_developer_request.json so
    the Developer agent has something to read. It does NOT change any code, so
    running the loop with this stub ends in needs_human_review.
    """
    _write_request(state)
    return _next_attempt(state)


# --- demo only ----------------------------------------------------------------
# These patterns undo the bugs seeded in fixtures/buggy_example.zip and
# fixtures/buggy_flow_example.zip. They exist so the full loop (fail, fix, pass)
# can be shown without the real Developer agent. Nothing here is a real fixer.
_SEEDED_BUGS = [
    ("crud_app/models.py",
     re.compile(r"\r?\n\r?\n    def save\(self, \*args, \*\*kwargs\):\r?\n        self\.price = self\.price \* 2\r?\n        super\(\)\.save\(\*args, \*\*kwargs\)"),
     ""),
    ("crud_app/views.py",
     re.compile(r"form\.save\(commit=False\)[^\r\n]*"),
     "form.save()"),
]


def demo_fixer_node(state: AgentState) -> AgentState:
    """DEMO ONLY: a mock Developer agent that undoes the seeded bugs in the fixtures."""
    root = Path(state["project"]["root_dir"]).resolve()
    for relative_path, pattern, replacement in _SEEDED_BUGS:
        target = root / relative_path
        if not target.is_file():
            continue
        original = target.read_text(encoding="utf-8")
        fixed = pattern.sub(replacement, original)
        if fixed != original:
            target.write_text(fixed, encoding="utf-8")
            print(f"[demo-fix] repaired {relative_path}", file=sys.stderr)
    return developer_feedback_node(state)
