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
    assert _engine.implement() is _engine.implement()
    assert _engine.create_pr() is _engine.create_pr()


def test_runner_singletons_match_orchestrator_phase_runners():
    """The implement/create_pr the tests load are the SAME objects the shared
    orchestrator binds in PHASE_RUNNERS — one engine object, not a per-test copy."""
    orch = _engine.orchestrator()
    assert orch.PHASE_RUNNERS["implement"] is _engine.implement().run
    assert orch.PHASE_RUNNERS["create_pr"] is _engine.create_pr().run
