"""Codex-main wiring: planner/implementer=codex + reviewer/rescue=claude resolves
to the REVERSED adapter pair (Codex writes the code, Claude reviews it), staying
cross-provider — pinned without any model calls.

The harness ships Claude-worker / Codex-reviewer; both adapters already run in
production every dogfood. "Codex main, Claude sub" reuses the SAME adapters with
swapped roles, so the only genuinely new surface is the role-swap wiring: that a
`[models]` flip resolves to the right adapter classes and still passes the
self-review guard. This is that regression. (The provider-family resolvers and the
guard's collapse cases are covered in test_adversarial_pairing_guard.py; here we pin
the concrete adapter-instance resolution end to end.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from adapters import (  # noqa: E402
    get_reviewer_adapter,
    get_worker_adapter,
    reviewer_provider,
    worker_provider,
)
from adapters.claude import ClaudeReviewerAdapter  # noqa: E402
from adapters.codex import CodexWorkerAdapter  # noqa: E402


def _orch():
    import _engine

    return _engine.orchestrator()


# A "Codex main, Claude sub" config — the reverse of the shipped default.
_CODEX_MAIN = {"planner": "codex", "implementer": "codex", "reviewer": "claude", "rescue": "claude"}


def _state() -> dict:
    return {"mode": "agent-pair", "models": dict(_CODEX_MAIN)}


def test_codex_main_resolves_to_the_codex_worker():
    assert isinstance(get_worker_adapter(_state()), CodexWorkerAdapter)
    assert worker_provider(_state()) == "codex"


def test_codex_main_resolves_to_the_claude_reviewer():
    adapter = get_reviewer_adapter(_state())
    assert isinstance(adapter, ClaudeReviewerAdapter)
    assert reviewer_provider(_state()) == "claude"


def test_codex_main_passes_the_adversarial_pairing_guard():
    # codex worker vs claude reviewer is genuinely cross-provider → no self-review.
    assert _orch()._adversarial_pairing_error(_state()) is None


def test_codex_main_is_cross_provider_and_is_the_default_reversed():
    s = _state()
    assert worker_provider(s) == "codex" and reviewer_provider(s) == "claude"
    assert worker_provider(s) != reviewer_provider(s)
    # sanity: this is exactly the reverse of the shipped default (claude / codex)
    assert worker_provider({}) == "claude" and reviewer_provider({}) == "codex"
