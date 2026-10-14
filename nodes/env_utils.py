from __future__ import annotations

import sys
from pathlib import Path


def venv_dir_for(root_dir: Path) -> Path:
    """Folder of the throwaway venv that belongs to the project at root_dir."""
    return root_dir.parent / f"_venv_{root_dir.name}"


def venv_bin_dir(venv_dir: Path) -> Path:
    """Scripts on Windows, bin on Linux/macOS."""
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin")


def venv_python(root_dir: Path) -> str:
    """Absolute path to the python executable inside the project's venv."""
    return str((venv_bin_dir(venv_dir_for(root_dir)) / "python").resolve())