"""Guard the shared engine loader (#54).

The order-dependent flake came from re-executing engine modules per test, which
created multiple module objects that polluted module-level state across tests.
`_engine` loads each engine module ONCE and shares the single instance; this test
pins that invariant so a future test can't reintroduce per-test re-exec.
"""

from __future__ import annotations

import _engine


def test_engine_modules_are_loaded_once_and_shared():
    assert _engine.orchestrator() is _engine.orchestrator()
    assert _engine.base() is _engine.base()
