from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nodes.env_utils import find_settings_module, venv_python
from state import AgentState, TestCaseSpec, TestSpec

TIMEOUT_SECONDS = 60

# How many levels of ForeignKey / OneToOneField the generator will follow when
# it has to create related objects before the object under test.
MAX_RELATION_DEPTH = 3

# Python source for a valid sample value of each supported field type. The
# DateTimeField and DecimalField entries are only defaults: _sample_expr()
# adjusts them to the project's USE_TZ setting and the field's digits.
_FIELD_SAMPLE_VALUES = {
    "CharField": '"sample text"',
    "TextField": '"sample text"',
    "BooleanField": "True",
    "IntegerField": "1",
    "FloatField": "1.0",
    "EmailField": '"test@example.com"',
    "SlugField": '"sample-slug"',
    "URLField": '"https://example.com"',
    "PositiveIntegerField": "1",
    "PositiveSmallIntegerField": "1",
    "PositiveBigIntegerField": "1",
    "SmallIntegerField": "1",
    "BigIntegerField": "1",
    "DateField": "datetime.date(2024, 1, 15)",
    "TimeField": "datetime.time(12, 30)",
    "DateTimeField": "datetime.datetime(2024, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)",
    "DecimalField": 'decimal.Decimal("1")',
    "UUIDField": 'uuid.UUID("12345678-1234-5678-1234-567812345678")',
}

_AUTO_TYPES = {"AutoField", "BigAutoField", "SmallAutoField"}
_RELATION_TYPES = {"ForeignKey", "OneToOneField"}

# Introspection runs once per project, inside the project's own venv. It lists
# every installed model (including django.contrib.auth and friends) because a
# ForeignKey may point outside the app under test, for example at auth.User.
# django.setup() does not touch the database.
_INTROSPECT_SCRIPT = """
import django, json, os, sys
sys.path.insert(0, __ROOT_DIR__)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", __SETTINGS_MODULE__)
django.setup()
from django.apps import apps
from django.conf import settings

models = []
for model in apps.get_models():
    meta = model._meta
    fields = []
    for f in meta.get_fields():
        if not hasattr(f, "get_internal_type"):
            continue
        if f.auto_created and not f.concrete:
            continue   # reverse side of a relation, not a column on this model
        try:
            internal_type = f.get_internal_type()
        except NotImplementedError:
            continue
        related = None
        if f.is_relation and f.related_model is not None:
            related = f.related_model._meta.label
        fields.append({
            "name": f.name,
            "type": internal_type,
            "related": related,
            "null": bool(f.null),
            "has_default": bool(f.has_default()),
            "empty_strings_allowed": bool(f.empty_strings_allowed),
            "auto_created": bool(f.auto_created),
            "auto_now": bool(getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)),
            "max_digits": getattr(f, "max_digits", None),
            "decimal_places": getattr(f, "decimal_places", None),
        })
    models.append({
        "label": meta.label,
        "model": model.__name__,
        "app_label": meta.app_label,
        "app_name": meta.app_config.name,
        "managed": bool(meta.managed),
        "proxy": bool(meta.proxy),
        "fields": fields,
    })
print(json.dumps({"use_tz": bool(settings.USE_TZ), "models": models}))
"""


def _introspect_project(python_exe: str, root_dir: Path, settings_module: str) -> dict:
    """Return {"use_tz": bool, "models": [model_info, ...]} for every installed model."""
    script = (
        _INTROSPECT_SCRIPT
        .replace("__ROOT_DIR__", repr(str(root_dir)))
        .replace("__SETTINGS_MODULE__", repr(settings_module))
    )
    result = subprocess.run(
        [python_exe, "-c", script], cwd=str(root_dir),
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Model introspection failed:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout)


def _sample_expr(field: dict, use_tz: bool) -> str:
    """Python source for a valid sample value of one supported field."""
    field_type = field["type"]
    if field_type == "DateTimeField" and not use_tz:
        return "datetime.datetime(2024, 1, 15, 12, 0)"
    if field_type == "DecimalField":
        # Pick a value that fits max_digits / decimal_places; Decimal("1") equals
        # Decimal("1.00"), so the round trip holds for any number of places.
        integer_digits = (field.get("max_digits") or 10) - (field.get("decimal_places") or 0)
        return 'decimal.Decimal("1")' if integer_digits >= 1 else 'decimal.Decimal("0.1")'
    return _FIELD_SAMPLE_VALUES[field_type]


def _var_name(label: str) -> str:
    return "rel_" + re.sub(r"\W+", "_", label).lower()


def _join_args(args: list[tuple[str, str]]) -> str:
    return ", ".join(f"{name}={expr}" for name, expr in args)


class _Plan:
    """How to build one valid instance of a model, or why that is not possible."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.setup_lines: list[str] = []          # create related objects first
        self.args: list[tuple[str, str]] = []     # kwargs for the model under test
        self.relation_vars: dict[str, str] = {}   # FK field name -> variable holding the related object
        self.uses: set[str] = set()               # labels of related models created in setup_lines
        self.imports: set[str] = set()            # stdlib modules the sample values need

    @property
    def ok(self) -> bool:
        return self.reason is None


class _Planner:
    """Builds a _Plan for a model, creating related objects recursively."""

    def __init__(self, models_by_label: dict[str, dict], use_tz: bool) -> None:
        self.models = models_by_label
        self.use_tz = use_tz

    def plan(self, label: str) -> _Plan:
        plan = _Plan()
        state = {"created": {}, "reason": None}
        args = self._args_for(label, (label,), plan, state)
        if args is None:
            plan.reason = state["reason"] or "could not build a valid object"
            return plan
        plan.args = args
        return plan

    def _args_for(self, label: str, stack: tuple[str, ...], plan: _Plan, state: dict):
        info = self.models.get(label)
        if info is None:
            state["reason"] = f"model {label} is not installed"
            return None
        if not info["managed"]:
            state["reason"] = f"{label} is unmanaged, so its table is not created in the test database"
            return None

        args: list[tuple[str, str]] = []
        for field in info["fields"]:
            field_type = field["type"]
            if (field["auto_created"] or field_type in _AUTO_TYPES
                    or field_type == "ManyToManyField" or field["auto_now"]):
                continue

            if field_type in _RELATION_TYPES:
                variable = self._related_variable(field, stack, plan, state)
                if variable is not None:
                    args.append((field["name"], variable))
                    if len(stack) == 1:
                        plan.relation_vars[field["name"]] = variable
                elif field["null"]:
                    state["reason"] = None   # optional relation: leave it empty
                else:
                    return None
                continue

            if field_type in _FIELD_SAMPLE_VALUES:
                expr = _sample_expr(field, self.use_tz)
                plan.imports.update(m for m in ("datetime", "decimal", "uuid") if f"{m}." in expr)
                args.append((field["name"], expr))
                continue

            # FileField, ImageField and other types we cannot fill: fine when the
            # column accepts an empty value, fatal when it does not.
            required = not field["null"] and not field["has_default"] and not field["empty_strings_allowed"]
            if required:
                state["reason"] = f"required field '{field['name']}' has unsupported type {field_type}"
                return None
        return args

    def _related_variable(self, field: dict, stack: tuple[str, ...], plan: _Plan, state: dict):
        related = field["related"]
        if related in state["created"]:
            return state["created"][related]
        if related in stack:
            state["reason"] = f"'{field['name']}' points back to {related} (cycle)"
            return None
        if len(stack) > MAX_RELATION_DEPTH:
            state["reason"] = f"'{field['name']}' is more than {MAX_RELATION_DEPTH} relations deep"
            return None
        args = self._args_for(related, stack + (related,), plan, state)
        if args is None:
            return None
        variable = _var_name(related)
        plan.setup_lines.append(f"{variable} = <<{related}>>.objects.create({_join_args(args)})")
        plan.uses.add(related)
        state["created"][related] = variable
        return variable


def _build_spec(app: str, model_info: dict, plan: _Plan) -> TestSpec:
    target = model_info["model"]
    if not plan.ok:
        return TestSpec(app=app, test_type="unit", target=target, cases=[], skipped_reason=plan.reason)

    cases: list[TestCaseSpec] = []
    for field in model_info["fields"]:
        name = field["name"]
        if name in plan.relation_vars:
            cases.append(TestCaseSpec(
                name=f"test_{name}_relation_round_trips",
                field=name,
                checks=f"creating a {target} linked to a related object through '{name}' and reading the link back",
            ))
        elif any(arg_name == name for arg_name, _ in plan.args) and field["type"] in _FIELD_SAMPLE_VALUES:
            cases.append(TestCaseSpec(
                name=f"test_{name}_round_trips",
                field=name,
                checks=f"creating a {target} with a sample {field['type']} value for '{name}' and reading it back",
            ))
    if not cases:
        cases.append(TestCaseSpec(
            name="test_can_be_created",
            field=None,
            checks=f"creating a {target} with valid values for every supported field",
        ))
    return TestSpec(app=app, test_type="unit", target=target, cases=cases)


def _render_test_class(spec: TestSpec, model_info: dict, plan: _Plan) -> str:
    """Render ONE TestCase class (no imports) for a single model.

    Fill every field we can so the object is valid; each test then asserts only
    on the one field it targets. Related objects are created first.
    """
    fields = {f["name"]: f for f in model_info["fields"]}
    samples = dict(plan.args)
    create_args = _join_args(plan.args)
    setup = [f"        {line}" for line in plan.setup_lines]

    lines = [
        f"class {spec['target']}ModelTests(TestCase):",
        '    """Auto-generated by the test generation agent. Review before relying on these."""',
        "",
    ]
    for case in spec["cases"]:
        lines += [f"    def {case['name']}(self):", *setup]
        lines += [
            f"        obj = {spec['target']}.objects.create({create_args})",
            "        obj.refresh_from_db()",
        ]
        field_name = case["field"]
        if field_name is None:
            lines.append("        self.assertIsNotNone(obj.pk)")
        elif field_name in plan.relation_vars:
            lines.append(f"        self.assertEqual(obj.{field_name}_id, {plan.relation_vars[field_name]}.pk)")
        else:
            lines.append(f"        self.assertEqual(obj.{field_name}, {samples[field_name]})")
        lines.append("")
    return "\n".join(lines)


def _name_models(local_labels: list[str], used_labels: set[str], models: dict[str, dict]):
    """Choose the Python name and import line for every model a test file refers to."""
    names: dict[str, str] = {}
    taken: set[str] = set()
    for label in local_labels:
        names[label] = models[label]["model"]
        taken.add(models[label]["model"])

    foreign_imports: dict[str, list[str]] = {}
    for label in sorted(used_labels - set(local_labels)):
        info = models[label]
        python_name = info["model"]
        import_name = python_name
        if python_name in taken:
            python_name = f"{info['model']}_{info['app_label']}"
            import_name = f"{info['model']} as {python_name}"
        taken.add(python_name)
        names[label] = python_name
        foreign_imports.setdefault(info["app_name"], []).append(import_name)

    import_lines = [f"from {module}.models import {', '.join(items)}" for module, items in sorted(foreign_imports.items())]
    return names, import_lines


def _render_app_test_file(rendered: list[tuple[TestSpec, dict, _Plan]], models: dict[str, dict]) -> str:
    """One file per app: a single import header plus every model's test class."""
    app_label = rendered[0][1]["app_label"]
    local_labels = [info["label"] for _, info, _ in rendered]
    used_labels: set[str] = set()
    stdlib: set[str] = set()
    for _, _, plan in rendered:
        used_labels |= plan.uses
        stdlib |= plan.imports
    # Related models of this app that have no test class of their own (a proxy,
    # for example) still come from .models.
    local_labels += sorted(label for label in used_labels
                           if models[label]["app_label"] == app_label and label not in local_labels)

    names, foreign_import_lines = _name_models(local_labels, used_labels, models)

    body = "\n\n".join(_render_test_class(spec, info, plan) for spec, info, plan in rendered)
    for label, python_name in names.items():
        body = body.replace(f"<<{label}>>", python_name)

    local_names = ", ".join(models[label]["model"] for label in local_labels)
    header = [f"import {module}" for module in sorted(stdlib)]
    if header:
        header.append("")
    header += ["from django.test import TestCase", "", f"from .models import {local_names}", *foreign_import_lines]
    return "\n".join(header) + "\n\n\n" + body + "\n"


def test_generation_node(state: AgentState) -> AgentState:
    """
    LangGraph node. Reads state["project"] + state["status"], writes
    state["test_specs"] and state["test_files"], and writes ONE generated
    test file per app onto disk (<app>/tests_generated.py).
    """
    if state.get("status") != "ready_for_test_generation":
        # Don't generate tests against a project that can't even boot.
        return state

    project = state["project"]
    root_dir = Path(project["root_dir"])
    python_exe = venv_python(root_dir)

    settings_module = find_settings_module(root_dir)
    if settings_module is None:
        raise RuntimeError("Could not locate settings.py to introspect models")

    registry = _introspect_project(python_exe, root_dir, settings_module)
    models = {m["label"]: m for m in registry["models"]}
    planner = _Planner(models, registry["use_tz"])

    test_specs: list[TestSpec] = []
    test_files: dict[str, str] = {}

    for app in project["django_apps"]:
        app_models = [m for m in registry["models"]
                      if m["app_label"] == app or m["app_name"].split(".")[-1] == app]
        rendered: list[tuple[TestSpec, dict, _Plan]] = []

        for model_info in app_models:
            if model_info["proxy"] or not model_info["managed"]:
                continue   # a proxy shares its parent's table; an unmanaged table is not created in tests
            plan = planner.plan(model_info["label"])
            spec = _build_spec(app, model_info, plan)
            test_specs.append(spec)
            if plan.ok:
                rendered.append((spec, model_info, plan))

        if not rendered:
            continue

        content = _render_app_test_file(rendered, models)
        rel_path = f"{app}/tests_generated.py"
        test_files[rel_path] = content
        (root_dir / rel_path).write_text(content)

    return {
        **state,
        "test_specs": test_specs,
        "test_files": test_files,
    }


test_generation_node.__test__ = False
