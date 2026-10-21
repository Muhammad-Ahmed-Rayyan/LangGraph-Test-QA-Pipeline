from __future__ import annotations

import re
from pathlib import Path

from nodes.env_utils import find_settings_module, venv_python
from nodes.integration_generation import (
    ACCEPTED_STATUSES, _group_of, _introspect_urls, _resolve_instance, _test_name_stem,
)
from nodes.test_generation import (
    _FILE_UPLOAD, _IMAGE_UPLOAD, _Planner, _introspect_project, _join_args, _name_models, _var_name, media_support,
)
from state import AgentState, TestCaseSpec, TestSpec

# Model field types whose value we can replace with a recognisable marker and
# then look for in a page or in the database.
_MARKER_TYPES = {"CharField", "TextField", "SlugField", "EmailField", "URLField"}
_MARKER_FORM_KINDS = {"char", "email", "url", "slug"}

# POST values for fields that are not markers, by form field kind.
_FORM_VALUES = {
    "integer": '"1"', "decimal": '"1"', "float": '"1.5"', "boolean": '"on"',
    "date": '"2024-01-15"', "datetime": '"2024-01-15 12:00:00"', "time": '"12:30:00"',
    "uuid": '"12345678-1234-5678-1234-567812345678"', "char": '"sample text"',
    "email": '"sample@example.com"', "url": '"https://example.com"', "slug": '"sample-slug"',
}
# The same idea for hand-written views that read request.POST keys that match model fields.
_MODEL_POST_VALUES = {
    "IntegerField": '"1"', "PositiveIntegerField": '"1"', "SmallIntegerField": '"1"',
    "PositiveSmallIntegerField": '"1"', "BigIntegerField": '"1"', "PositiveBigIntegerField": '"1"',
    "DecimalField": '"1"', "FloatField": '"1.5"', "BooleanField": '"on"', "DateField": '"2024-01-15"',
    "DateTimeField": '"2024-01-15 12:00:00"', "TimeField": '"12:30:00"',
    "UUIDField": '"12345678-1234-5678-1234-567812345678"',
}


def _marker_value(model_name: str, field: dict) -> str:
    words = f"system test {model_name} {field['name']}".replace("_", " ")
    slug = re.sub(r"\W+", "-", words).lower()
    if field["type"] == "SlugField":
        return slug
    if field["type"] == "EmailField":
        return f"{slug}@example.com"
    if field["type"] == "URLField":
        return f"https://{slug}.example.com"
    return words.capitalize()


class _Setup:
    """Collects the lines that create the objects one test needs."""

    def __init__(self, planner: _Planner) -> None:
        self.planner = planner
        self.lines: list[str] = []
        self.uses: set[str] = set()
        self.imports: set[str] = set()
        self.needs_files = self.needs_png = False
        self.reason: str | None = None

    def _add(self, line: str) -> None:
        if line not in self.lines:
            self.lines.append(line)

    def has(self, label: str) -> bool:
        return any(line.startswith(f"{_var_name(label)} = ") for line in self.lines)

    def ensure(self, label: str, displayed=None, exclude=()):
        """Create one instance of `label`. Returns (variable, marker) or (None, None).

        With `displayed`, one text field the page shows is given a recognisable
        marker value, so a test can look for it.
        """
        var = _var_name(label)
        plan = self.planner.plan(label)
        if not plan.ok:
            self.reason = f"cannot create a {label}: {plan.reason}"
            return None, None
        for line in plan.setup_lines:
            self._add(line)
        self.uses |= plan.uses | {label}
        self.imports |= plan.imports
        self.needs_files, self.needs_png = self.needs_files or plan.needs_files, self.needs_png or plan.needs_png
        if self.has(label):
            return var, None

        marker = None
        if displayed:
            info = self.planner.models[label]
            names = {name for name, _ in plan.args}
            for field in info["fields"]:
                if (field["type"] in _MARKER_TYPES and field["name"] in displayed and field["name"] in names
                        and field["name"] not in exclude and not field["primary_key"]):
                    marker = (field["name"], _marker_value(info["model"], field))
                    break
        args = [(name, repr(marker[1]) if marker and name == marker[0] else expr) for name, expr in plan.args]
        self._add(f"{var} = <<{label}>>.objects.create({_join_args(args)})")
        return var, marker


def _displayed(facts: dict) -> set[str]:
    attrs: set[str] = set()
    for names in (facts.get("templates") or {}).values():
        attrs |= set(names or [])
    return attrs


def _url_line(url: dict, var: str | None = None, attributes: dict | None = None) -> str:
    if url["name"] is None:
        return f"url = {url['literal_path']!r}"
    if not url["params"]:
        return f"url = reverse({url['name']!r})"
    kwargs = ", ".join(f"{param!r}: {var}.{attribute}" for param, attribute in attributes.items())
    return f"url = reverse({url['name']!r}, kwargs={{{kwargs}}})"


def _method(name: str, setup: _Setup, body: list[str]) -> str:
    lines = [f"    def {name}(self):", *[f"        {line}" for line in setup.lines], *[f"        {line}" for line in body], ""]
    return "\n".join(lines)


class _GroupBuilder:
    """Builds the system tests for the URLs of one app."""

    def __init__(self, group: str, group_urls: list[dict], group_models: list[dict], models: dict, planner: _Planner):
        self.group, self.urls, self.group_models = group, group_urls, group_models
        self.models, self.planner = models, planner
        self.methods: list[str] = []
        self.specs: list[TestSpec] = []
        self.uses: set[str] = set()
        self.imports: set[str] = set()
        self.uses_files = self.uses_png = False
        self.taken: set[str] = set()

    # -- bookkeeping ---------------------------------------------------------
    def _target(self, url: dict) -> str:
        return url["name"] or url["route"] or "/"

    def _skip(self, url: dict, what: str, reason: str) -> None:
        self.specs.append(TestSpec(app=self.group, test_type="system", target=f"{what} at {self._target(url)}",
                                   cases=[], skipped_reason=reason))

    def _done(self, url: dict, what: str, name: str, checks: str, setup: _Setup, body: list[str]) -> None:
        self.methods.append(_method(name, setup, body))
        self.uses |= setup.uses
        self.imports |= setup.imports
        self.uses_files, self.uses_png = self.uses_files or setup.needs_files, self.uses_png or setup.needs_png
        self.specs.append(TestSpec(app=self.group, test_type="system", target=f"{what} at {self._target(url)}",
                                   cases=[TestCaseSpec(name=name, field=None, checks=checks)]))

    def _reversible(self, url: dict) -> bool:
        return url["name"] is not None or url["literal_path"] is not None

    # -- pages that list or show stored objects -------------------------------
    def page(self, url: dict, stem: str) -> None:
        facts = url["facts"]
        what = "page"
        queried = [label for label in facts["models_queried"] if label in self.models]
        if not queried:
            return
        if facts["restricted"]:
            return self._skip(url, what, "the view filters or restricts what it shows (login, filter or request.user), "
                                         "so the page content cannot be predicted")
        if not url["supported"] or not self._reversible(url):
            return self._skip(url, what, "the URL cannot be reversed")
        setup, displayed, markers = _Setup(self.planner), _displayed(facts), []
        instance_var, attributes = None, None

        if url["params"]:
            model_info, outcome = _resolve_instance(url, self.group_models, self.models)
            if model_info is None:
                return self._skip(url, what, outcome)
            instance_var, marker = setup.ensure(model_info["label"], displayed, exclude=set(outcome.values()))
            attributes = outcome
            if instance_var is None:
                return self._skip(url, what, setup.reason)
            if marker:
                markers.append(marker[1])
            checks = f"GET {url['route']} for a stored {model_info['model']} renders the page"
        else:
            for label in queried:
                var, marker = setup.ensure(label, displayed)
                if var is not None and marker:
                    markers.append(marker[1])
            if not setup.lines:
                return self._skip(url, what, setup.reason or "no model the view reads could be created")
            checks = f"GET {url['route'] or '/'} renders the page with stored objects"
        if markers:
            checks += " and shows their text"

        body = [_url_line(url, instance_var, attributes),
                "response = self.client.get(url)",
                'self.assertEqual(response.status_code, 200, f"GET {url} returned {response.status_code}")']
        body += [f"self.assertContains(response, {marker!r})" for marker in markers]
        self._done(url, what, f"test_{stem}_page_shows_stored_objects", checks, setup, body)

    # -- forms ---------------------------------------------------------------
    def _form_data(self, form: dict, model_info: dict, setup: _Setup, exclude=()):
        """Build POST data for a ModelForm. Returns (entries, marker, error)."""
        fields = {f["name"]: f for f in model_info["fields"]}
        entries: list[tuple[str, str]] = []
        marker = None
        for field in form["fields"]:
            kind, name = field["kind"], field["name"]
            if kind is None:
                if field["required"]:
                    return None, None, f"required form field '{name}' has a type the generator cannot fill"
                continue
            if (marker is None and kind in _MARKER_FORM_KINDS and name not in exclude
                    and fields.get(name, {}).get("type") in _MARKER_TYPES):
                marker = (name, _marker_value(model_info["model"], fields[name]))
                entries.append((name, repr(marker[1])))
            elif kind in _FORM_VALUES:
                entries.append((name, _FORM_VALUES[kind]))
            elif kind in ("choice", "multi_choice"):
                if field["choice"] is None:
                    if field["required"]:
                        return None, None, f"required form field '{name}' has no usable choice"
                    continue
                value = repr(field["choice"])
                entries.append((name, value if kind == "choice" else f"[{value}]"))
            elif kind in ("model_choice", "model_multi"):
                var, _ = setup.ensure(field["related"])
                if var is None:
                    if field["required"]:
                        return None, None, setup.reason
                    setup.reason = None
                    continue
                entries.append((name, f"{var}.pk" if kind == "model_choice" else f"[{var}.pk]"))
            elif kind == "file":
                self.uses_files = True
                entries.append((name, _FILE_UPLOAD))
            elif kind == "image":
                self.uses_files = self.uses_png = True
                entries.append((name, _IMAGE_UPLOAD))
        return entries, marker, None

    @staticmethod
    def _data_lines(entries: list[tuple[str, str]]) -> list[str]:
        return ["data = {", *[f"    {name!r}: {expr}," for name, expr in entries], "}"]

    def _list_page_for(self, label: str, field: str):
        """A parameterless, unrestricted page that lists `label` and prints `field`."""
        for url in self.urls:
            facts = url["facts"]
            if (url["params"] or not url["supported"] or not self._reversible(url) or facts["restricted"]
                    or label not in facts["models_queried"] or field not in _displayed(facts)):
                continue
            return url
        return None

    def create_flow(self, url: dict, stem: str) -> None:
        facts, form, what = url["facts"], url["facts"]["form"], "create flow"
        model_info = self.models.get(form["model"]) if form["model"] else None
        if model_info is None:
            return self._skip(url, what, "the form is not a ModelForm for an installed model")
        if facts["restricted"]:
            return self._skip(url, what, "the view requires login or filters by user")
        if not (facts["has_save"] or "CreateView" in facts["bases"]) or facts["instance_form"]:
            return self._skip(url, what, "it is not clear that this view creates an object")
        setup = _Setup(self.planner)
        entries, marker, error = self._form_data(form, model_info, setup)
        if error:
            return self._skip(url, what, error)
        model_name = model_info["model"]
        lookup = f"filter({marker[0]}={marker[1]!r})" if marker else "all()"
        body = [
            _url_line(url), *self._data_lines(entries),
            "response = self.client.post(url, data)",
            "self.assertIn(response.status_code, ACCEPTED_STATUSES, f\"POST {url} returned {response.status_code}\")",
            f"self.assertEqual(<<{model_info['label']}>>.objects.{lookup}.count(), 1,",
            f"                 f\"POST {{url}} returned {{response.status_code}} but no {model_name} was saved\")",
        ]
        checks = f"POST valid data to {url['route'] or '/'} saves exactly one {model_name}"
        listing = self._list_page_for(model_info["label"], marker[0]) if marker else None
        if listing is not None:
            body += [f"listing = reverse({listing['name']!r})" if listing["name"] else f"listing = {listing['literal_path']!r}",
                     "response = self.client.get(listing)",
                     f"self.assertContains(response, {marker[1]!r})"]
            checks += " and the new object appears on the list page"
        setup.uses.add(model_info["label"])
        self._done(url, what, f"test_{stem}_flow_creates_object", checks, setup, body)

    def update_with_form(self, url: dict, stem: str) -> None:
        facts, form, what = url["facts"], url["facts"]["form"], "update flow"
        model_info, outcome = _resolve_instance(url, self.group_models, self.models)
        if model_info is None:
            return self._skip(url, what, outcome)
        if facts["restricted"]:
            return self._skip(url, what, "the view requires login or filters by user")
        if form["model"] != model_info["label"]:
            return self._skip(url, what, "the form's model differs from the model the URL refers to")
        setup = _Setup(self.planner)
        var, _ = setup.ensure(model_info["label"])
        if var is None:
            return self._skip(url, what, setup.reason)
        entries, marker, error = self._form_data(form, model_info, setup, exclude=set(outcome.values()))
        if error:
            return self._skip(url, what, error)
        if marker is None:
            return self._skip(url, what, "the form has no text field whose change can be checked")
        body = [
            _url_line(url, var, outcome), *self._data_lines(entries),
            "response = self.client.post(url, data)",
            "self.assertIn(response.status_code, ACCEPTED_STATUSES, f\"POST {url} returned {response.status_code}\")",
            f"{var}.refresh_from_db()",
            f"self.assertEqual({var}.{marker[0]}, {marker[1]!r}, f\"POST {{url}} returned {{response.status_code}} but the object was not updated\")",
        ]
        self._done(url, what, f"test_{stem}_flow_updates_object",
                   f"POST new data to {url['route']} changes '{marker[0]}' on the stored {model_info['model']}", setup, body)

    def delete_flow(self, url: dict, stem: str) -> None:
        facts, what = url["facts"], "delete flow"
        if facts["restricted"]:
            return self._skip(url, what, "the view requires login or filters by user")
        setup = _Setup(self.planner)
        if url["params"]:
            model_info, outcome = _resolve_instance(url, self.group_models, self.models)
            if model_info is None:
                return self._skip(url, what, outcome)
            var, _ = setup.ensure(model_info["label"])
            post, url_args = "self.client.post(url)", (var, outcome)
        else:
            model_info = self.models.get(facts["pk_model"] or "")
            if model_info is None:
                return self._skip(url, what, "cannot tell which model the view deletes")
            var, _ = setup.ensure(model_info["label"])
            post, url_args = f"self.client.post(url, {{{facts['pk_key']!r}: {var}.pk}})", (None, None)
        if var is None:
            return self._skip(url, what, setup.reason)
        body = [
            _url_line(url, *url_args) if url["params"] else _url_line(url), f"pk = {var}.pk",
            f"response = {post}",
            "self.assertIn(response.status_code, ACCEPTED_STATUSES, f\"POST {url} returned {response.status_code}\")",
            f"self.assertFalse(<<{model_info['label']}>>.objects.filter(pk=pk).exists(),",
            "                 f\"POST {url} returned {response.status_code} but the object still exists\")",
        ]
        self._done(url, what, f"test_{stem}_flow_deletes_object",
                   f"POST to {url['route'] or '/'} removes the stored {model_info['model']}", setup, body)

    def update_with_post_keys(self, url: dict, stem: str) -> None:
        facts, what = url["facts"], "update flow"
        model_info = self.models.get(facts["pk_model"] or "")
        if model_info is None:
            return self._skip(url, what, "cannot tell which model the view updates")
        if facts["restricted"]:
            return self._skip(url, what, "the view requires login or filters by user")
        fields = {f["name"]: f for f in model_info["fields"]}
        data: list[tuple[str, str]] = []
        changed: list[tuple[str, str]] = []
        for key in facts["post_keys"]:
            if key == facts["pk_key"]:
                continue
            field = fields.get(key)
            if field is None:
                return self._skip(url, what, f"POST key '{key}' is not a field of {model_info['model']}")
            if field["type"] in _MARKER_TYPES:
                value = _marker_value(model_info["model"], field)
                changed.append((key, value))
                data.append((key, repr(value)))
            elif field["type"] in _MODEL_POST_VALUES:
                data.append((key, _MODEL_POST_VALUES[field["type"]]))
            else:
                return self._skip(url, what, f"POST key '{key}' has unsupported type {field['type']}")
        if not changed:
            return self._skip(url, what, "the view reads no text field whose change can be checked")
        setup = _Setup(self.planner)
        var, _ = setup.ensure(model_info["label"])
        if var is None:
            return self._skip(url, what, setup.reason)
        body = [
            _url_line(url),
            "data = {", f"    {facts['pk_key']!r}: {var}.pk,", *[f"    {k!r}: {v}," for k, v in data], "}",
            "response = self.client.post(url, data)",
            "self.assertIn(response.status_code, ACCEPTED_STATUSES, f\"POST {url} returned {response.status_code}\")",
            f"{var}.refresh_from_db()",
        ]
        body += [f"self.assertEqual({var}.{k}, {v!r}, f\"POST {{url}} returned {{response.status_code}} but '{k}' was not updated\")"
                 for k, v in changed]
        self._done(url, what, f"test_{stem}_flow_updates_object",
                   f"POST new values to {url['route'] or '/'} changes {', '.join(k for k, _ in changed)} on the stored {model_info['model']}",
                   setup, body)

    # -- one URL -------------------------------------------------------------
    def build(self) -> None:
        for url in self.urls:
            facts = url["facts"]
            if not url["supported"] or facts is None or "error" in facts:
                continue
            stem = _test_name_stem(url, self.taken)
            self.page(url, stem)
            has_form = facts["form"] is not None
            if has_form and not url["params"]:
                self.create_flow(url, stem)
            elif has_form and (facts["instance_form"] or "UpdateView" in facts["bases"]):
                self.update_with_form(url, stem)
            if "DeleteView" in facts["bases"] and url["params"]:
                self.delete_flow(url, stem)
            elif not url["params"] and not has_form and facts["pk_key"]:
                if facts["has_delete"]:
                    self.delete_flow(url, stem)
                elif facts["has_save"]:
                    self.update_with_post_keys(url, stem)


def _class_name(group: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"\W+", group) if part) + "SystemTests"


def _render_file(group: str, builder: _GroupBuilder, models: dict) -> str:
    names, import_lines = _name_models([], builder.uses, models)
    body = "\n".join(builder.methods)
    for label, python_name in names.items():
        body = body.replace(f"<<{label}>>", python_name)

    media_stdlib, media_django, media_constants, decorator, cleanup = media_support(builder.uses_files, builder.uses_png)
    header = [f"import {module}" for module in sorted(builder.imports | media_stdlib)]
    if header:
        header.append("")
    django_test = "Client, TestCase, override_settings" if builder.uses_files else "Client, TestCase"
    header += [*media_django, f"from django.test import {django_test}", "from django.urls import reverse", *import_lines]
    return (
        "\n".join(header) + f"\n\nACCEPTED_STATUSES = {ACCEPTED_STATUSES!r}\n"
        + "".join(f"{line}\n" for line in media_constants) + "\n\n"
        + f"{decorator}class {_class_name(group)}(TestCase):\n"
          '    """Auto-generated by the system test agent: complete user flows across URLs, forms and the database."""\n\n'
        + cleanup
        + "    def setUp(self):\n"
          "        # A failing view should surface as a failed assertion with its status code.\n"
          "        self.client = Client(raise_request_exception=False)\n\n"
        + body
    )


def system_generation_node(state: AgentState) -> AgentState:
    """
    LangGraph node. Adds system tests (full user flows across URLs, forms and the
    database) to state["test_specs"] and state["test_files"], and writes
    <app>/tests_system_generated.py for each app where a flow can be inferred.
    Never replaces what the earlier generators wrote.
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
    by_group: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for url in urls:
        group = _group_of(url, groups)
        key = (url["name"], url["route"])
        if group is None or key in seen:
            continue
        seen.add(key)
        by_group.setdefault(group, []).append(url)

    new_specs: list[TestSpec] = []
    new_files: dict[str, str] = {}
    for group, group_urls in sorted(by_group.items()):
        group_models = [m for m in registry["models"]
                        if (m["app_label"] == group or m["app_name"].split(".")[-1] == group)
                        and m["managed"] and not m["proxy"]]
        builder = _GroupBuilder(group, group_urls, group_models, models, planner)
        builder.build()
        new_specs += builder.specs
        if not builder.methods:
            continue
        content = _render_file(group, builder, models)
        rel_path = f"{group}/tests_system_generated.py"
        new_files[rel_path] = content
        (root_dir / rel_path).write_text(content)

    return {
        **state,
        "test_specs": list(state.get("test_specs", [])) + new_specs,
        "test_files": {**state.get("test_files", {}), **new_files},
    }


system_generation_node.__test__ = False
