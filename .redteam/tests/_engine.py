"""Shared, cached loaders for the engine modules under test (#54).

Re-executing an engine module on every `_load_*()` call (a fresh
`spec_from_file_location` + `exec_module` per test) created MULTIPLE module
objects for the same source and made test outcomes order-dependent — the #54
flake. Load each engine module ONCE here, registered in `sys.modules` per the
documented importlib recipe (the registration is required so the module can
resolve its own submodule imports during exec), and reuse the single instance.

Tests still isolate via `monkeypatch` (auto-reverted after each test), which
works identically on a shared module — an audit confirmed no test mutates these
modules outside `monkeypatch`. Sharing one instance is what removes the
"multiple module objects pollute module-level state" root cause by construction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

_cache: dict[str, ModuleType] = {}


def _load(name: str, path: Path) -> ModuleType:
    cached = _cache.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # register BEFORE exec (documented recipe)
    spec.loader.exec_module(module)
    _cache[name] = module
    return module


def orchestrator() -> ModuleType:
    return _load("redteam_orchestrator", _WF / "orchestrator.py")


def base() -> ModuleType:
    return _load("redteam_phase_base", _WF / "phase_runners" / "_base.py")
