# Integrating with other agents

## Calling the agent

```python
from testing_qa import run_testing_qa

result = run_testing_qa("generated_project.zip", max_retries=3, developer_node=my_developer_agent)
result["status"]       # completed | needs_human_review | environment_error
result["outcome"]      # tests_passed | tests_failed | blocked | execution_failed
result["report"]       # the full report, shaped like docs/report_example.json
result["report_file"]  # reports/<project>_report.json
```

Input: a `.zip` or an extracted folder holding a Django project (code only, or code plus documentation).
Output: the report. Nothing in the project is changed by the testing agent. To embed the graph in a larger LangGraph
application, call `graph.build_graph(...)`. It returns a compiled graph that takes `{"zip_path": ...}` or `{"work_dir": ...}`.

## What the Developer agent receives

When a run fails, the report goes back to the Developer agent: as `state["report_json"]` inside the graph, and as the file
`reports/<project>_developer_request.json`. Fields it can act on:

- `outcome`: `tests_failed` or `blocked`.
- `faults[]`: `category`, `message`, `suspected_location` (a file in the project), `traceback`, `confidence` and `affected_tests`. Fix `high` confidence faults first.
- `setup.errors[]`: why a `blocked` project would not boot.
- `production.findings[]`: severity, message and file, when the tests passed.

## Connecting the real Developer agent

Pass a function as `developer_node`. It receives the state and must:

1. read `state["report_json"]` and change the code under `state["project"]["root_dir"]`;
2. return `nodes.developer_stub._next_attempt(state)`, which counts the retry and clears the old results.

After it returns, the loop re-enters at `runnability_check` for a `blocked` project (dependencies may have changed) and at
`test_generation` for `tests_failed`. It stops after `max_retries` and ends with status `needs_human_review`.

## Questions to settle with the other teams

1. What does the Developer agent hand over: a zip, a folder or a path in shared state?
2. Does the system call this agent as a Python function, as a LangGraph subgraph, or over HTTP? (A small endpoint around `run_testing_qa` is enough for HTTP.)
3. Can the Developer agent read the report as its fix input, or does it need another shape?
4. Which Python version and LLM provider does the rest of the system use?
