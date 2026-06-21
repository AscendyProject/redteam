"""Each review round's full prose is preserved round-numbered, not one-deep (#86).

`_clear_manual_phase_artifacts` runs before a new round writes a fresh review.
It used to move `code_review.md` -> `code_review.md.previous`, so a 3+ round loop
kept only the latest two rounds' text. Now it accumulates `code_review.round1.md`,
`code_review.round2.md`, ... while the latest stays at `code_review.md`.
"""

from __future__ import annotations


def _orch():
    import _engine

    return _engine.orchestrator()


def test_review_rounds_accumulate_round_numbered(tmp_path):
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    cr = task_dir / "code_review.md"

    # Round 1 review written, then cleared as round 2 begins.
    cr.write_text("round-1 prose", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "review_code")
    assert not cr.exists()  # slot vacated for the next round
    assert (task_dir / "code_review.round1.md").read_text(encoding="utf-8") == "round-1 prose"

    # Round 2 review written, then cleared as round 3 begins.
    cr.write_text("round-2 prose", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "review_code")
    assert (task_dir / "code_review.round1.md").read_text(encoding="utf-8") == "round-1 prose"
    assert (task_dir / "code_review.round2.md").read_text(encoding="utf-8") == "round-2 prose"

    # Round 3 stays as the latest at the canonical name (existing contract).
    cr.write_text("round-3 prose", encoding="utf-8")
    assert cr.read_text(encoding="utf-8") == "round-3 prose"
    # No round1 prose was lost across the 3-round loop.
    assert (task_dir / "code_review.round1.md").read_text(encoding="utf-8") == "round-1 prose"


def test_plan_and_rescue_review_rounds_are_numbered_independently(tmp_path):
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()

    (task_dir / "plan_review.md").write_text("plan-1", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "plan_review")
    (task_dir / "rescue_report.md").write_text("rescue-1", encoding="utf-8")
    orch._clear_manual_phase_artifacts(task_dir, "rescue")

    # Each review file keeps its own round counter.
    assert (task_dir / "plan_review.round1.md").read_text(encoding="utf-8") == "plan-1"
    assert (task_dir / "rescue_report.round1.md").read_text(encoding="utf-8") == "rescue-1"
    # No legacy single-depth .previous is produced any more.
    assert not (task_dir / "plan_review.md.previous").exists()


def test_clear_is_noop_when_no_review_present(tmp_path):
    orch = _orch()
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    # No code_review.md on disk (e.g. first round) -> nothing archived, no error.
    orch._clear_manual_phase_artifacts(task_dir, "review_code")
    assert not (task_dir / "code_review.round1.md").exists()
