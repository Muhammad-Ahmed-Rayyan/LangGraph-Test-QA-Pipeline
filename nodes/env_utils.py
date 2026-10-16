from __future__ import annotations

import sys
from pathlib import Path


def find_settings_module(root_dir: Path) -> str | None:
    """Locate the dotted Django settings module from a settings.py file."""
    for settings_file in sorted(root_dir.rglob("settings.py")):
        relative = settings_file.relative_to(root_dir)
        if len(relative.parts) > 1:
            return ".".join(relative.parts[:-1] + ("settings",))
        return "settings"
    return None


def venv_dir_for(root_dir: Path) -> Path:
    """Folder of the throwaway venv that belongs to the project at root_dir."""
    return root_dir.parent / f"_venv_{root_dir.name}"


def venv_bin_dir(venv_dir: Path) -> Path:
    """Scripts on Windows, bin on Linux/macOS."""
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin")


def venv_python(root_dir: Path) -> str:
    """Absolute path to the python executable inside the project's venv."""
    return str((venv_bin_dir(venv_dir_for(root_dir)) / "python").resolve())