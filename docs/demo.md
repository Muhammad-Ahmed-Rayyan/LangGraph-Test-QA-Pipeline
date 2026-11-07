# Demo script (about 10 minutes, plus waiting)

Each run installs packages into a fresh environment and takes a few minutes. Start the first run before you start talking.

1. **Clean project.** `python graph.py fixtures/runnable_example.zip`. Show `tests_passed`, 17 of 17 (3 unit, 8 integration, 6 system), then the `production` block: `not_ready` because the settings contain a secret key and a database password, shown by file and line only.
2. **Deployment files.** Open `reports/Django-crud_private-main_deployment/`: Dockerfile, compose file, `.env.example`, `DEPLOYMENT.md`. Say they are checked statically and never built.
3. **Project that will not boot.** `python graph.py fixtures/non_runnable_example.zip`. Show `blocked` and the real `ModuleNotFoundError`; no tests were written.
4. **Real bug.** `python graph.py fixtures/buggy_example.zip`. One fault: `test_price_round_trips`, `AssertionError: 2 != 1`, location `crud_app/models.py`, `confidence: high`.
5. **The loop.** `python graph.py fixtures/buggy_example.zip --demo-fix`. Fail, mock fix, pass on attempt 2. Say the mock stands in for the Developer agent.
6. **Honest reporting.** Open `docs/report_example.json`: skipped tests, `confidence`, grouped faults. Say a green result is never claimed when tests were skipped.
7. **Close.** `docs/architecture.md` diagram, then `docs/integration.md` for the connection to the other teams.
