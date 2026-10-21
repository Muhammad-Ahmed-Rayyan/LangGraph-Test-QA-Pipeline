from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nodes.env_utils import find_settings_module, venv_python
from nodes.test_generation import (
    _Planner, _name_models, _introspect_project, _join_args, media_support, TIMEOUT_SECONDS,
)
from state import AgentState, TestCaseSpec, TestSpec

# Status codes that mean "the page answered sensibly". 404 and 500 are the
# failures this generator looks for. 401, 403 and 405 are accepted because a
# view that demands permission or only allows POST is behaving as designed.
ACCEPTED_STATUSES = (200, 301, 302, 401, 403, 405)

_INTEGER_FIELD_TYPES = {
    "AutoField", "BigAutoField", "SmallAutoField", "IntegerField", "BigIntegerField",
    "SmallIntegerField", "PositiveIntegerField", "PositiveSmallIntegerField", "PositiveBigIntegerField",
}

# Runs inside the project's venv. django.setup() does not touch the database.
_URL_SCRIPT = r"""
import django, functools, json, os, sys
sys.path.insert(0, __ROOT_DIR__)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", __SETTINGS_MODULE__)
django.setup()
from django.urls import get_resolver
from django.urls.resolvers import RegexPattern, RoutePattern, URLPattern, URLResolver

import ast, inspect, re, textwrap
from django.db.models import Model
from django.forms import ModelForm
from django.forms.models import modelform_factory
from django.template.loader import get_template

FORM_KINDS = [
    ("ImageField", "image"), ("FileField", "file"), ("ModelMultipleChoiceField", "model_multi"),
    ("ModelChoiceField", "model_choice"), ("MultipleChoiceField", "multi_choice"), ("ChoiceField", "choice"),
    ("NullBooleanField", "boolean"), ("BooleanField", "boolean"), ("DecimalField", "decimal"),
    ("FloatField", "float"), ("IntegerField", "integer"), ("EmailField", "email"), ("URLField", "url"),
    ("SlugField", "slug"), ("UUIDField", "uuid"), ("DateTimeField", "datetime"), ("DateField", "date"),
    ("TimeField", "time"), ("CharField", "char"),
]


def model_label(obj):
    if isinstance(obj, type) and issubclass(obj, Model) and not obj._meta.abstract:
        return obj._meta.label
    return None


def form_info(form_class):
    form = form_class()
    fields = []
    for name, field in form.fields.items():
        names = [c.__name__ for c in type(field).__mro__]
        kind = next((k for cls, k in FORM_KINDS if cls in names), None)
        entry = {"name": name, "kind": kind, "required": bool(field.required), "related": None, "choice": None}
        if kind in ("model_choice", "model_multi"):
            entry["related"] = field.queryset.model._meta.label
        if kind in ("choice", "multi_choice"):
            try:
                for value, label in field.choices:
                    if isinstance(label, (list, tuple)):
                        value = next((v for v, _ in label if v not in ("", None)), None)
                    if value not in ("", None):
                        entry["choice"] = value if isinstance(value, (str, int)) else str(value)
                        break
            except Exception:
                pass
        if getattr(field, "disabled", False):
            entry["kind"] = None
            entry["required"] = False
        fields.append(entry)
    return {"model": model_label(getattr(form_class._meta, "model", None)), "fields": fields}


def template_attrs(name):
    # Attributes the template prints without a filter, like {{ item.name }}.
    try:
        source = get_template(name).template.source
    except Exception:
        return None
    plain, filtered = set(), set()
    for expression in re.findall(r"{{(.*?)}}", source, flags=re.S):
        match = re.match(r"\s*\w+\.(\w+)\s*(\|.*)?$", expression.strip() + " ", flags=re.S)   # one level only: item.name, not item.author.name
        if match:
            (filtered if match.group(2) else plain).add(match.group(1))
    return sorted(plain - filtered)


class Facts(ast.NodeVisitor):
    def __init__(self, scope):
        self.scope = scope
        self.models, self.forms = [], []
        self.templates, self.post_keys, self.file_keys = [], [], []
        self.has_delete = self.has_save = self.restricted = self.instance_form = False
        self.post_vars, self.pk_key, self.pk_model = {}, None, None

    def _add(self, items, value):
        if value is not None and value not in items:
            items.append(value)

    def _post_key(self, node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            holder, args = node.func.value, node.args
            if isinstance(holder, ast.Attribute) and holder.attr in ("POST", "FILES") and args \
                    and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                return holder.attr, args[0].value
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                and node.value.attr in ("POST", "FILES") and isinstance(node.slice, ast.Constant):
            return node.value.attr, node.slice.value
        return None

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            text = ast.unparse(decorator)
            if any(word in text for word in ("login_required", "permission_required", "user_passes_test", "staff_member_required")):
                self.restricted = True
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == "objects" and isinstance(node.value, ast.Name):
            self._add(self.models, model_label(self.scope.get(node.value.id)))
        if node.attr == "user" and isinstance(node.value, ast.Name) and node.value.id == "request":
            self.restricted = True
        self.generic_visit(node)

    def visit_Name(self, node):
        candidate = self.scope.get(node.id)
        if isinstance(candidate, type) and issubclass(candidate, ModelForm):
            self._add(self.forms, node.id)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value.endswith(".html"):
            self._add(self.templates, node.value)

    def visit_Assign(self, node):
        found = self._post_key(node.value)
        if found and found[0] == "POST":
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.post_vars[target.id] = found[1]
        self.generic_visit(node)

    def visit_Call(self, node):
        found = self._post_key(node)
        if found:
            self._add(self.post_keys if found[0] == "POST" else self.file_keys, found[1])
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "delete":
                self.has_delete = True
            elif attr == "save":
                self.has_save = True
            elif attr in ("filter", "exclude"):
                self.restricted = True
            elif attr == "get":
                for keyword in node.keywords:
                    if keyword.arg in ("pk", "id") and isinstance(keyword.value, ast.Name) \
                            and keyword.value.id in self.post_vars and isinstance(node.func.value, ast.Attribute) \
                            and isinstance(node.func.value.value, ast.Name):
                        self.pk_key = self.post_vars[keyword.value.id]
                        self.pk_model = model_label(self.scope.get(node.func.value.value.id))
        if isinstance(node.func, ast.Name) and node.func.id in ("get_object_or_404", "get_list_or_404") and node.args \
                and isinstance(node.args[0], ast.Name):
            self._add(self.models, model_label(self.scope.get(node.args[0].id)))
        if isinstance(node.func, ast.Name) and any(k.arg == "instance" for k in node.keywords):
            self.instance_form = True
        self.generic_visit(node)


def view_facts(callback):
    try:
        view_class = getattr(callback, "view_class", None)
        if view_class is not None:
            target, scope = view_class, vars(inspect.getmodule(view_class))
        else:
            target = inspect.unwrap(callback)
            scope = target.__globals__
        tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        facts = Facts(scope)
        facts.visit(tree)
        bases, form_class, templates = [], None, list(facts.templates)
        if view_class is not None:
            bases = [c.__name__ for c in view_class.__mro__]
            model = getattr(view_class, "model", None)
            if model is None and getattr(view_class, "queryset", None) is not None:
                model = view_class.queryset.model
            facts._add(facts.models, model_label(model))
            if any(b in bases for b in ("LoginRequiredMixin", "PermissionRequiredMixin", "UserPassesTestMixin")):
                facts.restricted = True
            form_class = getattr(view_class, "form_class", None)
            if not (isinstance(form_class, type) and issubclass(form_class, ModelForm)):
                form_class = None   # DeleteView carries a plain Form, which says nothing about a model
            if form_class is None and model is not None and getattr(view_class, "fields", None) is not None \
                    and ("CreateView" in bases or "UpdateView" in bases):
                form_class = modelform_factory(model, fields=view_class.fields)
            if getattr(view_class, "template_name", None):
                templates.append(view_class.template_name)
            elif model is not None:
                suffix = "_detail" if "DetailView" in bases else "_list" if "ListView" in bases else None
                if suffix:
                    templates.append(f"{model._meta.app_label}/{model._meta.model_name}{suffix}.html")
        elif facts.forms:
            form_class = scope[facts.forms[0]]
        displayed = {name: template_attrs(name) for name in templates}
        return {
            "models_queried": facts.models, "form": form_info(form_class) if form_class else None,
            "templates": displayed, "post_keys": facts.post_keys, "file_keys": facts.file_keys,
            "pk_key": facts.pk_key, "pk_model": facts.pk_model, "has_delete": facts.has_delete,
            "has_save": facts.has_save, "restricted": facts.restricted, "bases": bases,
            "instance_form": facts.instance_form,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


found = []


def view_model_label(callback):
    view_class = getattr(callback, "view_class", None)
    if view_class is None:
        return None
    model = getattr(view_class, "model", None)
    queryset = getattr(view_class, "queryset", None)
    if model is None and queryset is not None:
        model = getattr(queryset, "model", None)
    return model._meta.label if model is not None and hasattr(model, "_meta") else None


def pattern_params(pattern):
    # Returns (params, supported). Regex patterns are reported but not supported.
    if isinstance(pattern, RoutePattern):
        return {k: type(v).__name__ for k, v in pattern.converters.items()}, True
    if isinstance(pattern, RegexPattern):
        groups = pattern.regex.groups
        return {k: "regex" for k in pattern.regex.groupindex}, groups == 0
    return {}, False


def walk(patterns, namespaces, route_parts, params, supported, literal_ok):
    for item in patterns:
        item_params, item_supported = pattern_params(item.pattern)
        if isinstance(item, URLResolver):
            ns = namespaces + ([item.namespace] if item.namespace else [])
            is_route = isinstance(item.pattern, RoutePattern) and not item.pattern.converters
            walk(item.url_patterns, ns, route_parts + [str(item.pattern)],
                 {**params, **item_params}, supported and item_supported, literal_ok and is_route)
        elif isinstance(item, URLPattern):
            callback = item.callback
            while isinstance(callback, functools.partial):
                callback = callback.func
            all_params = {**params, **item_params}
            is_route = isinstance(item.pattern, RoutePattern) and not item.pattern.converters
            literal = None
            if literal_ok and is_route and not all_params:
                literal = "/" + "".join(route_parts) + str(item.pattern)
            found.append({
                "name": ":".join(namespaces + [item.name]) if item.name else None,
                "route": "".join(route_parts) + str(item.pattern),
                "literal_path": literal,
                "params": all_params,
                "supported": supported and item_supported,
                "view_module": getattr(callback, "__module__", "") or "",
                "view_name": getattr(callback, "__name__", "") or "",
                "view_model": view_model_label(callback),
                "facts": view_facts(callback),
            })


walk(get_resolver().url_patterns, [], [], {}, True, True)
print(json.dumps(found))
"""


def _introspect_urls(python_exe: str, root_dir: Path, settings_module: str) -> list[dict]:
    script = (
        _URL_SCRIPT
        .replace("__ROOT_DIR__", repr(str(root_dir)))
        .replace("__SETTINGS_MODULE__", repr(settings_module))
    )
    result = subprocess.run(
        [python_exe, "-c", script], cwd=str(root_dir),
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"URL introspection failed:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout)


def _group_of(url: dict, groups: set[str]) -> str | None:
    """The app (or project package) whose code defines the view, or None for admin, static and third-party URLs."""
    top = url["view_module"].split(".")[0]
    return top if top in groups else None


def _resolve_instance(url: dict, group_models: list[dict], models: dict[str, dict]):
    """Pick the model a parameterized URL refers to and how to read each parameter from an instance.

    Returns (model_info, {param: attribute}) or (None, reason).
    """
    by_name = {m["model"].lower(): m for m in group_models}
    chosen = models.get(url["view_model"]) if url["view_model"] else None

    if chosen is None:
        for param in url["params"]:
            base = re.sub(r"_(id|pk)$", "", param)
            if base != param and base in by_name:
                chosen = by_name[base]
                break
    if chosen is None:
        mentioned = [m for key, m in by_name.items() if key in url["view_name"].lower()]
        if len(mentioned) == 1:
            chosen = mentioned[0]
    if chosen is None and len(group_models) == 1:
        chosen = group_models[0]
    if chosen is None:
        return None, "cannot tell which model the URL parameters refer to"

    fields = {f["name"]: f for f in chosen["fields"]}
    pk_field = next((f for f in chosen["fields"] if f["primary_key"]), None)
    attributes = {}
    for param, converter in url["params"].items():
        if param in ("pk", "id") or re.search(r"_(id|pk)$", param):
            attribute, source = "pk", pk_field
        elif param in fields:
            attribute, source = param, fields[param]
        else:
            return None, f"parameter '{param}' does not match a field of {chosen['model']}"
        source_type = source["type"] if source else None
        if converter == "IntConverter" and source_type not in _INTEGER_FIELD_TYPES:
            return None, f"parameter '{param}' is an int but {chosen['model']}.{attribute} is {source_type}"
        if converter == "UUIDConverter" and source_type != "UUIDField":
            return None, f"parameter '{param}' is a uuid but {chosen['model']}.{attribute} is {source_type}"
        attributes[param] = attribute
    return chosen, attributes


def _test_name_stem(url: dict, taken: set[str]) -> str:
    raw = url["name"] or (url["literal_path"] or url["route"]).strip("/") or "root"
    stem = re.sub(r"\W+", "_", raw).strip("_").lower() or "root"
    candidate, counter = stem, 2
    while candidate in taken:
        candidate = f"{stem}_{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


def _login_user_lines(registry: dict, planner: _Planner):
    """Lines that create a plain (non-staff) user to log in with, or None with a reason."""
    label = registry.get("auth_user_model")
    if not label:
        return None, "django.contrib.auth is not installed"
    plan = planner.plan(label)
    if not plan.ok:
        return None, plan.reason
    overrides = {"is_staff": "False", "is_superuser": "False"}
    username_field = registry.get("username_field")
    user_info = planner.models[label]
    fields = {f["name"]: f for f in user_info["fields"]}
    if username_field in fields and fields[username_field]["type"] in ("CharField", "EmailField", "SlugField"):
        overrides[username_field] = '"integration_user"' if fields[username_field]["type"] != "EmailField" else '"integration@example.com"'
    if "email" in fields and "email" != username_field and fields["email"]["type"] == "EmailField":
        overrides["email"] = '"integration@example.com"'
    args = [(name, overrides.get(name, expr)) for name, expr in plan.args]
    line = f"user = <<{label}>>.objects.create({_join_args(args)})"
    return (plan, line), None


def _render_method(stem: str, url: dict, instance, login, logged_in: bool) -> str:
    """One test method. `instance` is (plan, model_info, attributes) or None; `login` is (plan, line)."""
    lines: list[str] = []
    setup: list[str] = []
    if instance is not None:
        plan, model_info, _ = instance
        setup += plan.setup_lines
        setup.append(f"obj = <<{model_info['label']}>>.objects.create({_join_args(plan.args)})")
    if logged_in:
        setup += login[0].setup_lines
        setup.append(login[1])
    seen, ordered = set(), []
    for line in setup:                      # the same related object is created only once
        if line not in seen:
            seen.add(line)
            ordered.append(line)

    suffix = "_when_logged_in" if logged_in else ""
    lines.append(f"    def test_{stem}_get_responds{suffix}(self):")
    lines += [f"        {line}" for line in ordered]
    if logged_in:
        lines.append("        self.client.force_login(user)")
    if url["name"] is None:
        lines.append(f"        url = {url['literal_path']!r}")
    elif instance is None:
        lines.append(f"        url = reverse({url['name']!r})")
    else:
        kwargs = ", ".join(f"{param!r}: obj.{attribute}" for param, attribute in instance[2].items())
        lines.append(f"        url = reverse({url['name']!r}, kwargs={{{kwargs}}})")
    lines += [
        "        response = self.client.get(url)",
        "        self.assertIn(",
        "            response.status_code, ACCEPTED_STATUSES,",
        '            f"GET {url} returned {response.status_code}",',
        "        )",
        "",
    ]
    return "\n".join(lines)


def _class_name(group: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"\W+", group) if part) + "IntegrationTests"


def integration_generation_node(state: AgentState) -> AgentState:
    """
    LangGraph node. Adds integration tests (views, URLs and the database together,
    through Django's test client) to state["test_specs"] and state["test_files"],
    and writes <app>/tests_integration_generated.py for each app that has views.
    Never replaces what the unit generator wrote.
    """
    if state.get("status") != "ready_for_test_generation":
        return state

    project = state["project"]
    root_dir = Path(project["root_dir"])
    python_exe = venv_python(root_dir)
    settings_module = find_settings_module(root_dir)
    if settings_module is None:
        raise RuntimeError("Could not locate settings.py to introspect URLs")

    registry = _introspect_project(python_exe, root_dir, settings_module)
    models = {m["label"]: m for m in registry["models"]}
    planner = _Planner(models, registry["use_tz"], fill_files=True)
    urls = _introspect_urls(python_exe, root_dir, settings_module)

    groups = set(project["django_apps"]) | {settings_module.split(".")[0]}
    login, login_reason = _login_user_lines(registry, planner)

    new_specs: list[TestSpec] = []
    new_files: dict[str, str] = {}
    seen_urls: set[tuple] = set()

    by_group: dict[str, list[dict]] = {}
    for url in urls:
        group = _group_of(url, groups)
        key = (url["name"], url["route"])
        if group is None or key in seen_urls:
            continue
        seen_urls.add(key)
        by_group.setdefault(group, []).append(url)

    for group, group_urls in sorted(by_group.items()):
        group_models = [m for m in registry["models"]
                        if (m["app_label"] == group or m["app_name"].split(".")[-1] == group)
                        and m["managed"] and not m["proxy"]]
        taken: set[str] = set()
        methods: list[str] = []
        used_labels: set[str] = set()
        stdlib: set[str] = set()
        needs_files = needs_png = False

        for url in group_urls:
            target = url["name"] or url["route"] or "/"

            def skip(reason: str) -> None:
                new_specs.append(TestSpec(app=group, test_type="integration", target=target,
                                          cases=[], skipped_reason=reason))

            if not url["supported"]:
                skip("regex URL patterns with parameters are not supported")
                continue
            if url["name"] is None and url["literal_path"] is None:
                skip("unnamed URL with parameters cannot be reversed")
                continue

            instance = None
            if url["params"]:
                model_info, outcome = _resolve_instance(url, group_models, models)
                if model_info is None:
                    skip(outcome)
                    continue
                plan = planner.plan(model_info["label"])
                if not plan.ok:
                    skip(f"cannot create a {model_info['model']}: {plan.reason}")
                    continue
                instance = (plan, model_info, outcome)
                used_labels |= plan.uses | {model_info["label"]}
                stdlib |= plan.imports
                needs_files, needs_png = needs_files or plan.needs_files, needs_png or plan.needs_png

            stem = _test_name_stem(url, taken)
            cases = [TestCaseSpec(
                name=f"test_{stem}_get_responds", field=None,
                checks=f"GET {url['route'] or '/'} as an anonymous visitor does not return 404 or 500",
            )]
            methods.append(_render_method(stem, url, instance, login, logged_in=False))
            if login is not None:
                cases.append(TestCaseSpec(
                    name=f"test_{stem}_get_responds_when_logged_in", field=None,
                    checks=f"GET {url['route'] or '/'} as a logged-in user does not return 404 or 500",
                ))
                methods.append(_render_method(stem, url, instance, login, logged_in=True))
                used_labels |= login[0].uses | {registry["auth_user_model"]}
                stdlib |= login[0].imports
                needs_files, needs_png = needs_files or login[0].needs_files, needs_png or login[0].needs_png
            new_specs.append(TestSpec(app=group, test_type="integration", target=target, cases=cases))

        if not methods:
            continue

        names, import_lines = _name_models([], used_labels, models)
        body = "\n".join(methods)
        for label, python_name in names.items():
            body = body.replace(f"<<{label}>>", python_name)

        media_stdlib, media_django, media_constants, decorator, cleanup = media_support(needs_files, needs_png)
        header = [f"import {module}" for module in sorted(stdlib | media_stdlib)]
        if header:
            header.append("")
        django_test = "Client, TestCase, override_settings" if needs_files else "Client, TestCase"
        header += [*media_django, f"from django.test import {django_test}", "from django.urls import reverse", *import_lines]
        content = (
            "\n".join(header)
            + "\n\n# 404 and 500 are the failures. 401, 403 and 405 are accepted because a view that\n"
              "# demands permission or only allows POST is behaving as designed.\n"
              f"ACCEPTED_STATUSES = {ACCEPTED_STATUSES!r}\n"
            + "".join(f"{line}\n" for line in media_constants) + "\n\n"
            + f"{decorator}class {_class_name(group)}(TestCase):\n"
              '    """Auto-generated by the integration test agent. Review before relying on these."""\n\n'
            + cleanup
            + "    def setUp(self):\n"
              "        # A failing view should surface as a failed assertion with its status code.\n"
              "        self.client = Client(raise_request_exception=False)\n\n"
            + body
        )
        rel_path = f"{group}/tests_integration_generated.py"
        new_files[rel_path] = content
        (root_dir / rel_path).write_text(content)

    return {
        **state,
        "test_specs": list(state.get("test_specs", [])) + new_specs,
        "test_files": {**state.get("test_files", {}), **new_files},
    }


integration_generation_node.__test__ = False
