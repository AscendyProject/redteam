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


def test_review_prompts_are_channel_aware_for_read_only_adapter() -> None:
    """#144: the headless review adapter runs `codex exec --sandbox read-only` and
    captures the review from stdout, but the prompt files instructed an unconditional
    file write + `.done` sentinel — impossible under read-only, so a strict Codex
    burned turns / stalled to timeout and fell back to manual. The `## Output` section
    must make the channel explicit: read-only adapter → stdout only, no writes; the
    file-write + `.done` sentinel is qualified as the MANUAL fallback path only.
    """
    for name, sentinel in (("code_review.md", "code_review.done"), ("plan_review.md", "plan_review.done")):
        body = _read(name)
        assert "read-only" in body  # the adapter's sandbox is named
        assert "stdout" in body  # the adapter's actual output channel
        # the write + sentinel instruction must be scoped to the manual fallback,
        # not stated unconditionally (which contradicts the read-only adapter path)
        assert "manual fallback" in body
        assert sentinel in body  # the manual path still documents the sentinel


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
