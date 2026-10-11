from __future__ import annotations

import os
import shutil
import zipfile

from state import AgentState, ProjectProfile

DOC_FILENAMES = {"readme.md", "readme.rst", "readme.txt", "readme", "srs.md", "srs.pdf"}


def _extract_zip(zip_path: str, dest_dir: str) -> str:
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    return dest_dir


def _find_project_root(extracted_dir: str) -> str:
    entries = [e for e in os.listdir(extracted_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1 and os.path.isdir(os.path.join(extracted_dir, entries[0])):
        return os.path.join(extracted_dir, entries[0])
    return extracted_dir


def _scan_project(root_dir: str) -> ProjectProfile:
    doc_files: list[str] = []
    manage_py_path: str | None = None
    requirements_file: str | None = None
    django_apps: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "venv", ".venv", "node_modules")]

        for fname in filenames:
            lower = fname.lower()
            full_path = os.path.join(dirpath, fname)

            if lower in DOC_FILENAMES:
                doc_files.append(os.path.relpath(full_path, root_dir))

            if fname == "manage.py" and manage_py_path is None:
                manage_py_path = full_path

            if fname == "requirements.txt" and requirements_file is None:
                requirements_file = full_path

            if fname == "models.py":
                app_dir = os.path.basename(dirpath)
                if app_dir not in django_apps:
                    django_apps.append(app_dir)

    return ProjectProfile(
        root_dir=root_dir,
        has_docs=len(doc_files) > 0,
        doc_files=doc_files,
        is_django_project=manage_py_path is not None,
        manage_py_path=manage_py_path,
        django_apps=django_apps,
        requirements_file=requirements_file,
    )


def ingestion_node(state: AgentState) -> AgentState:
    if state.get("zip_path"):
        work_dir = state.get("work_dir") or (state["zip_path"] + "_extracted")
        extracted = _extract_zip(state["zip_path"], work_dir)
        root_dir = _find_project_root(extracted)
    elif state.get("work_dir"):
        root_dir = _find_project_root(state["work_dir"])
    else:
        raise ValueError("AgentState must set either 'zip_path' or 'work_dir'")

    root_dir = os.path.abspath(root_dir)
    profile = _scan_project(root_dir)

    return {
        **state,
        "work_dir": root_dir,
        "project": profile,
    }