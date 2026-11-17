"""Refresh only this plugin's modules when a scoped source update is loaded."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def ensure_current(
    package: str,
    main_module: str,
    entry_path: str,
    checks=(),
    version=None,
) -> None:
    """Drop stale owned imports before the main module imports its dependencies."""
    parent = sys.modules.get(package)
    if parent is None or not package:
        return
    root = Path(entry_path).resolve().parent
    digest = hashlib.sha256()
    excluded = {"tests", "data", ".git", "node_modules", ".venv", "__pycache__"}
    try:
        sources = sorted(
            root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError:
        return
    for path in sources:
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        try:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        except OSError:
            return
    if version is not None:
        digest.update(str(version).encode())
    fingerprint = digest.hexdigest()
    recorded = getattr(parent, "_dynamics_games_fingerprint", None)
    stale = recorded is not None and recorded != fingerprint
    if not stale:
        for mod_rel, cls_name, attr_name in checks:
            mod = sys.modules.get(f"{package}.{mod_rel}")
            if mod is not None:
                cls = getattr(mod, cls_name, None)
                if cls is None or not hasattr(cls, attr_name):
                    stale = True
                    break
    if stale:
        prefix = package + "."
        for mod_name in list(sys.modules):
            if mod_name != main_module and mod_name.startswith(prefix):
                mod = sys.modules.pop(mod_name, None)
                sub = mod_name[len(prefix) :].split(".", 1)[0]
                if hasattr(parent, sub) and getattr(parent, sub) is mod:
                    try:
                        delattr(parent, sub)
                    except AttributeError:
                        pass
    parent._dynamics_games_fingerprint = fingerprint
