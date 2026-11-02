from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nodes.env_utils import find_settings_module, venv_python
from nodes.report import write_report
from nodes.test_execution import _write_test_settings
from state import AgentState

TIMEOUT_SECONDS = 120
MAX_FILE_BYTES = 1_000_000

# Django check ids that are serious enough to block a release on their own.
_HIGH_CHECK_IDS = {"security.W018", "security.W009"}   # DEBUG is True, SECRET_KEY is weak
_DEPLOY_LINE_RE = re.compile(
    r"^(?P<obj>\S.*?):\s+\((?P<id>[A-Za-z_]+\.[A-Z]\d+)\)\s+(?P<msg>.+)$", re.MULTILINE
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "migrations", "static", "staticfiles", "media",
              "venv", ".venv", "env", "site-packages"}
_SCAN_SUFFIXES = {".py", ".ini", ".cfg", ".yml", ".yaml", ".toml", ".json", ".env"}
_GENERATED_FILE_RE = re.compile(r"^(?:tests(?:_[^.]+)?_generated|test_settings)\.py$")

_PLACEHOLDER_PASSWORDS = {"password", "pass", "secret", "changeme", "xxxx", "yourpassword", "your-password"}
_SECRET_PATTERNS = [
    ("url_with_credentials", re.compile(r"""[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@'"]+:[^\s@'"]+@[^\s'"]+"""),
     "A URL with a username and password inside it"),
    ("hardcoded_secret_key", re.compile(r"""\bSECRET_KEY\s*=\s*['"][^'"\n]{8,}['"]"""),
     "SECRET_KEY is a literal string in the source"),
    ("hardcoded_password", re.compile(r"""['"]PASSWORD['"]\s*:\s*['"][^'"\n]+['"]"""),
     "A database PASSWORD is a literal string"),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), "An AWS access key id"),
    ("api_key_literal", re.compile(r"""(?i)\b(?:api[_-]?key|token|secret)\w*\s*=\s*['"][A-Za-z0-9_\-]{20,}['"]"""),
     "An API key or token as a literal string"),
]

_WEB_SERVERS = {"gunicorn", "uvicorn", "daphne", "waitress", "hypercorn", "uwsgi"}

# Run inside the project's venv. Prints settings values only as flags and names, never secrets.
_SETTINGS_SCRIPT = """
import django, json, os, sys
sys.path.insert(0, __ROOT_DIR__)
os.environ["DJANGO_SETTINGS_MODULE"] = __SETTINGS_MODULE__
django.setup()
from django.conf import settings
print(json.dumps({
    "allowed_hosts": [str(host) for host in settings.ALLOWED_HOSTS],
    "static_root": str(settings.STATIC_ROOT or ""),
    "staticfiles_installed": "django.contrib.staticfiles" in settings.INSTALLED_APPS,
    "database_engines": {alias: cfg.get("ENGINE", "") for alias, cfg in settings.DATABASES.items()},
}))
"""


def _finding(severity: str, category: str, message: str, location: str = "") -> dict:
    return {"severity": severity, "category": category, "message": message, "location": location}


def _run(command: list[str], cwd: Path) -> tuple[int | None, str]:
    """Run one command. Returns (exit code or None, combined output); never raises."""
    try:
        completed = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {TIMEOUT_SECONDS} seconds"
    except OSError as exc:
        return None, str(exc)
    return completed.returncode, (completed.stdout or "") + "\n" + (completed.stderr or "")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --- the checks ---------------------------------------------------------------


def _deploy_severity(check_id: str) -> str:
    if check_id in _HIGH_CHECK_IDS or check_id.split(".")[-1][:1] in ("E", "C"):
        return "high"
    return "medium" if check_id.startswith("security.") else "low"


def _check_deploy(python: str, manage_py: str, root: Path, settings_module: str) -> list[dict]:
    code, output = _run([python, manage_py, "check", "--deploy", f"--settings={settings_module}"], root)
    if code is None:
        return [_finding("medium", "deploy_check", f"manage.py check --deploy did not finish: {output}")]
    findings, seen = [], set()
    for match in _DEPLOY_LINE_RE.finditer(output):
        check_id = match.group("id")
        if check_id in seen:
            continue
        seen.add(check_id)
        findings.append(_finding(_deploy_severity(check_id), "deploy_check",
                                 f"[{check_id}] {match.group('msg').strip()}", settings_module))
    if not findings and code != 0:
        findings.append(_finding("medium", "deploy_check", f"manage.py check --deploy failed: {_last_line(output)}"))
    return findings


def _check_migrations(python: str, manage_py: str, root: Path, test_settings_module: str) -> list[dict]:
    code, output = _run([python, manage_py, "makemigrations", "--check", "--dry-run",
                         f"--settings={test_settings_module}"], root)
    if code == 0:
        return []
    # Exit code 1 means "changes found" but also "Django crashed", so look for Django's own line.
    apps = re.findall(r"^Migrations for '([^']+)':", output, re.MULTILINE)
    if code == 1 and apps:
        return [_finding("high", "migrations",
                         f"Model changes in {', '.join(apps)} are not covered by a migration. Deploying would "
                         "leave the database without those tables or columns. Run makemigrations and commit the files.",
                         f"{apps[0]}/migrations/")]
    reason = output if code is None else _last_line(output)
    return [_finding("medium", "migrations", f"Could not check migrations: {reason}")]


def _check_settings(python: str, root: Path, settings_module: str) -> list[dict]:
    script = (_SETTINGS_SCRIPT.replace("__ROOT_DIR__", repr(str(root)))
              .replace("__SETTINGS_MODULE__", repr(settings_module)))
    code, output = _run([python, "-c", script], root)
    if code != 0:
        reason = output if code is None else _last_line(output)
        return [_finding("medium", "settings", f"Settings could not be inspected: {reason}", settings_module)]
    try:
        values = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [_finding("medium", "settings", "Settings could not be inspected: unreadable output", settings_module)]

    findings = []
    # An empty ALLOWED_HOSTS is already reported by Django's own check (security.W020).
    if "*" in values["allowed_hosts"]:
        findings.append(_finding("medium", "settings", "ALLOWED_HOSTS allows every host ('*'). Set it to the host "
                                 "names the project is served from.", settings_module))
    if values["staticfiles_installed"] and not values["static_root"]:
        findings.append(_finding("medium", "settings", "STATIC_ROOT is not set, so collectstatic cannot gather "
                                 "static files for a web server.", settings_module))
    for alias, engine in values["database_engines"].items():
        if "sqlite3" in engine:
            findings.append(_finding("low", "settings", f"Database '{alias}' is SQLite. That is fine for a demo "
                                     "but not for several server processes.", settings_module))
    return findings


def _should_scan(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts[:-1]
    if any(part in _SKIP_DIRS or part.startswith("_venv_") for part in relative_parts):
        return False
    if _GENERATED_FILE_RE.match(path.name):
        return False
    if path.name.startswith(".env") or (path.name.startswith("requirements") and path.suffix == ".txt"):
        return True
    return path.suffix in _SCAN_SUFFIXES and path.name != "package-lock.json"


def _is_real_credential_url(text: str) -> bool:
    credentials = text.split("://", 1)[-1].split("@", 1)[0]
    password = credentials.partition(":")[2]
    return bool(password) and password[0] not in "${<%" and password.lower() not in _PLACEHOLDER_PASSWORDS


def _scan_secrets(root: Path) -> list[dict]:
    """Find secrets written into the source. Only the kind, file and line are reported, never the value."""
    hits: dict[tuple[str, str], list[int]] = {}
    descriptions = {kind: text for kind, _, text in _SECRET_PATTERNS}
    env_files: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _should_scan(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        if path.name.startswith(".env"):
            env_files.append(relative)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            for kind, pattern, _ in _SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if kind == "url_with_credentials" and not _is_real_credential_url(match.group(0)):
                    continue
                hits.setdefault((kind, relative), []).append(number)
                break   # one finding per line: a SECRET_KEY line would otherwise also match the generic token pattern

    findings = []
    for (kind, relative), numbers in hits.items():
        shown = ", ".join(str(n) for n in numbers[:5]) + (" and more" if len(numbers) > 5 else "")
        findings.append(_finding("high", "secrets",
                                 f"{descriptions[kind]} ({relative}, line {shown}). Move it to an environment "
                                 "variable and treat the exposed value as compromised.", relative))
    for relative in env_files:
        findings.append(_finding("medium", "secrets",
                                 f"An environment file ({relative}) is part of the project. It usually holds "
                                 "secrets and should not be shipped.", relative))
    return findings


def _check_requirements(requirements_file: str | None) -> list[dict]:
    if not requirements_file:
        return [_finding("medium", "dependencies", "No requirements file, so a deployment cannot install the dependencies.")]
    path = Path(requirements_file)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    names, unpinned = set(), []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        name = re.split(r"[=<>~!\[;\s]", line, maxsplit=1)[0].lower().replace("_", "-")
        names.add(name)
        pinned = "==" in line or "===" in line or "~=" in line or (">=" in line and "<" in line)
        if not pinned:
            unpinned.append(name)
    findings = []
    if unpinned:
        shown = ", ".join(unpinned[:10]) + (" and more" if len(unpinned) > 10 else "")
        findings.append(_finding("low", "dependencies", f"{len(unpinned)} package(s) are not pinned to a version "
                                 f"({shown}). A later install can pull in different code.", path.name))
    if not names & _WEB_SERVERS:
        findings.append(_finding("medium", "dependencies", "No production web server (gunicorn, uvicorn, daphne, "
                                 "waitress...) is listed. Django's development server must not be used in production.", path.name))
    return findings


def _find_entrypoints(root: Path) -> dict[str, str | None]:
    found: dict[str, str | None] = {"wsgi": None, "asgi": None}
    for kind in found:
        for path in sorted(root.rglob(f"{kind}.py")):
            parts = path.relative_to(root).parts
            if any(part in _SKIP_DIRS or part.startswith("_venv_") for part in parts[:-1]):
                continue
            found[kind] = ".".join(parts)[:-3]
            break
    return found


def _verdict(findings: list[dict]) -> str:
    if any(f["severity"] == "high" for f in findings):
        return "not_ready"
    return "ready_with_warnings" if findings else "ready"


def production_readiness_node(state: AgentState) -> AgentState:
    """
    LangGraph node. Reads state["report_json"] and state["project"]. When the tests
    passed, it adds report["production"] and writes the report file again.
    """
    report = state.get("report_json") or {}
    project = state.get("project") or {}
    if report.get("outcome") != "tests_passed" or not project.get("is_django_project"):
        return state

    root = Path(project["root_dir"]).resolve()
    python = venv_python(root)
    settings_module = find_settings_module(root)
    manage_py = project.get("manage_py_path")
    if settings_module is None or not manage_py:
        return state
    manage_py = str(Path(manage_py).resolve())

    findings: list[dict] = []
    checks_run: list[str] = []

    findings += _check_deploy(python, manage_py, root, settings_module)
    checks_run.append("django_deploy_check")
    try:
        test_settings_module = _write_test_settings(root, settings_module)
        findings += _check_migrations(python, manage_py, root, test_settings_module)
    except OSError as exc:
        findings.append(_finding("medium", "migrations", f"Could not check migrations: {exc}"))
    checks_run.append("migrations")
    findings += _check_settings(python, root, settings_module)
    checks_run.append("settings")
    findings += _scan_secrets(root)
    checks_run.append("secrets")
    findings += _check_requirements(project.get("requirements_file"))
    checks_run.append("dependencies")

    entrypoint = _find_entrypoints(root)
    if not entrypoint["wsgi"] and not entrypoint["asgi"]:
        findings.append(_finding("high", "entrypoint", "No wsgi.py or asgi.py was found, so a web server has "
                                 "nothing to start the project with."))
    checks_run.append("entrypoint")

    for number, finding in enumerate(findings, start=1):
        finding["id"] = f"P{number}"
    production = {
        "verdict": _verdict(findings),
        "checks_run": checks_run,
        "findings": [{"id": f["id"], **{k: v for k, v in f.items() if k != "id"}} for f in findings],
        "entrypoint": entrypoint,
        "artifacts": [],
    }

    new_report = {**report, "production": production}
    write_report(new_report, root.name)
    return {**state, "report_json": new_report, "production_report": production}