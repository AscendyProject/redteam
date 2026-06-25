"""#97 — the review prompts must mandate an output-validity / anti-degeneracy check.

The agent-pair review verified conformance, safety, and test integrity but never
whether a discriminating output (score / grade / ranking / classification) actually
discriminates — so a rubric that saturates to one grade for everyone passed review.
These pin that both the code-review and plan-review prompts now carry the
anti-degeneracy mandate, so it can't silently regress. They fail against pre-change
prompts (which had no such mandate).
"""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

_PROMPTS = Path(__file__).resolve().parents[1] / "prompts" / "codex"


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8").lower()


def test_code_review_prompt_mandates_output_validity() -> None:
    body = _read("code_review.md")
    # names the degeneracy failure mode and the conformance-is-not-enough point
    assert "discriminat" in body  # "discriminating" / "discriminates"
    assert "saturat" in body
    assert "degenerate" in body
    # conformance to the spec must not excuse a degenerate design
    assert "conformance" in body


def test_plan_review_prompt_flags_saturating_specs() -> None:
    body = _read("plan_review.md")
    assert "saturat" in body
    assert "discriminat" in body
    assert "degenerate" in body


def test_standalone_review_prompt_keeps_output_validity_active() -> None:
    """#97 review (IR-001): the standalone `review` surface SUSPENDS code_review.md's
    Required Checks (#103), so a new diff-level check placed there would be skipped on
    a first-class path. The output-validity check must be re-enumerated in the
    standalone prompt's diff-level criteria so it stays active outside the pipeline."""
    import _engine

    orch = _engine.orchestrator()

    class _Proj:
        base_branch = "main"
        security_checklist = ".redteam/docs/security-checklist.md"
        context_file = ".redteam/docs/project-context.md"

    class _Cfg:
        project = _Proj()

    prompt = orch._standalone_review_prompt(_Cfg()).lower()
    assert "discriminat" in prompt
    assert "saturat" in prompt or "degenerate" in prompt
