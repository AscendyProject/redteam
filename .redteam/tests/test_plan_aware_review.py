"""Tests for the plan-fidelity Required Check (#133).

The check existed before this change as one unstructured line — "Check that the
implementation matches the approved outcome.md" — and in ~19 observed review
rounds it never produced a single Done-when-item finding; every finding was
diff-level. The rewrite makes the check structured: locate the Done-when list,
adjudicate EACH item on its own line as met or unmet (mirroring how carried-over
findings are adjudicated in a narrowed round), flag an unmet item severity:major,
and fall back to the Goal statement explicitly when no list exists.

Clause C eligibility for .redteam/prompts/codex/code_review.md is established by
the audit recorded in the test-quality-gate goal: the file has exactly four
in-repo consumers — review_code.py:42, review_code.py:117, orchestrator.py:2133,
orchestrator.py:452 — all of which embed only the file's *path* as a string.
None open, read, parse, or interpret its contents within harness code. The
markdown assertions here therefore ride Clause C's per-artifact exemption.

The built-prompt and standalone-prompt assertions call the actual prompt
builders and assert on their assembled output; they are reachability guards for
behaviour that predates this diff, so they are paired with discriminating
markdown assertions inside a single test rather than standing alone (the lesson
repeated through #173/#177/#178: an assertion that also holds pre-change cannot
be its own regression test).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import phase_runners.review_code as _rc  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CODE_REVIEW_MD = _REPO_ROOT / ".redteam/prompts/codex/code_review.md"
_TD_PATH = Path("/tmp/batch/tasks/task-001")
_BASE = "main"


def _required_checks_section() -> str:
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    start = text.find("## Required Checks")
    assert start != -1
    end = text.find("## ", start + 1)
    return text[start:end] if end != -1 else text[start:]


def _load_orchestrator():
    import _engine

    return _engine.orchestrator()


# ---------------------------------------------------------------------------
# The structured plan-fidelity clause (markdown pins — Clause C)
# ---------------------------------------------------------------------------


def test_required_checks_demand_per_item_done_when_adjudication():
    """#133: the plan check must name the Done-when list and demand per-item
    verdicts, not a vague "matches the plan" pass.

    Pins the semantic load-bearing phrases, not cosmetic wording. Fails against
    pre-change code, where the check was one line and the string "Done-when"
    appeared nowhere in the file.
    """
    section = _required_checks_section()
    assert "Done-when" in section
    # Per-item, not in aggregate — the phrase that forbids summarizing.
    assert "each Done-when item" in section
    # The verdict vocabulary for items, mirroring narrowed-round adjudication.
    assert "met or unmet" in section
    # An unmet item has teeth.
    fidelity_para = section[section.find("Plan fidelity") : section.find("would have failed")]
    assert "severity:major" in fidelity_para
    # The no-list case is explicit, never silent.
    assert "Goal statement" in fidelity_para
    assert "silently skip" in fidelity_para


def test_plan_fidelity_is_task_scoped_and_reachable():
    """The new check reaches in-pipeline reviews and cannot leak into standalone.

    One claim in three parts, asserted together because only the first
    discriminates:
    1. the clause is labelled task-scoped (new — fails pre-change);
    2. both built pipeline prompts still route the reviewer to outcome.md and the
       criteria file, so the clause is actually reachable (unchanged guard);
    3. the standalone prompt still suspends outcome.md alignment, so the
       strengthened check cannot make `orchestrator review` unapprovable again —
       the #103 failure mode (unchanged guard).
    """
    # 1 — discriminating half.
    section = _required_checks_section()
    assert "task-scoped" in section[section.find("Plan fidelity") :][:200]

    # 2 — reachability: full and narrowed prompts.
    full = _rc._code_review_prompt(_TD_PATH, _BASE)
    assert "outcome.md" in full
    assert ".redteam/prompts/codex/code_review.md" in full
    narrowed = _rc._narrowed_code_review_prompt(
        _TD_PATH, _BASE, "deadbeef", [{"id": "IR-001", "severity": "major", "status": "open", "summary": "x"}]
    )
    assert "outcome.md" in narrowed
    assert ".redteam/prompts/codex/code_review.md" in narrowed

    # 3 — the standalone prompt still suspends outcome.md-alignment checks.
    orch = _load_orchestrator()
    cfg = SimpleNamespace(
        project=SimpleNamespace(base_branch="main", security_checklist="sec.md", context_file="ctx.md")
    )
    standalone = orch._standalone_review_prompt(cfg)
    assert "outcome.md alignment" in standalone
    assert "do not apply outside the pipeline" in standalone
