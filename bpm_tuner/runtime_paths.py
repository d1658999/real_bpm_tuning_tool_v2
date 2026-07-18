from __future__ import annotations

import sys
from pathlib import Path


def application_root() -> Path:
    """Return the folder containing the deployed application and external data."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def bundled_root() -> Path:
    """Return PyInstaller's extraction folder, or the application folder in source runs."""

    extraction_root = getattr(sys, "_MEIPASS", None)
    return Path(extraction_root).resolve() if extraction_root else application_root()


def resolve_from_root(path: str | Path, root: str | Path | None = None) -> Path:
    """Resolve a user/data path relative to the deployed application folder."""

    value = Path(path).expanduser()
    if value.is_absolute():
        return value.resolve()
    return (Path(root).resolve() if root is not None else application_root()).joinpath(value).resolve()


def bundled_resource(path: str | Path) -> Path:
    """Resolve a resource embedded in a PyInstaller one-file executable."""

    return resolve_from_root(path, bundled_root())
