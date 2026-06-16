"""Task scaffolding — `orchestrator.py new <batch-dir> <slug> [--title]` (#55).

Creates the next task-NNN dir and seeds input.md from the template, so adding a
task no longer means hand-reproducing the brief structure the planner depends on.
"""

from __future__ import annotations

import _engine

orch = _engine.orchestrator()


def test_new_task_seeds_input_from_template(tmp_path):
    batch = tmp_path / "batch"
    rc = orch.cmd_new_task(batch, ["my-feature", "--title", "My Feature"])
    assert rc == 0
    task_dir = batch / "tasks" / "task-001-my-feature"
    assert task_dir.is_dir()
    body = (task_dir / "input.md").read_text(encoding="utf-8")
    assert body.startswith("# My Feature")  # title substituted
    for header in ("## Goal", "## Affected files", "## Verification", "## Out of scope", "## Risks"):
        assert header in body  # the brief contract the outcome-planner reads


def test_new_task_picks_next_number(tmp_path):
    batch = tmp_path / "batch"
    (batch / "tasks" / "task-001-a").mkdir(parents=True)
    (batch / "tasks" / "task-004-b").mkdir(parents=True)
    orch.cmd_new_task(batch, ["c"])
    assert (batch / "tasks" / "task-005-c").is_dir()  # max(1,4)+1


def test_new_task_slugifies(tmp_path):
    batch = tmp_path / "batch"
    orch.cmd_new_task(batch, ["Fix the Bug!!"])
    assert (batch / "tasks" / "task-001-fix-the-bug").is_dir()


def test_new_task_derives_slug_from_title_when_no_positional(tmp_path):
    batch = tmp_path / "batch"
    orch.cmd_new_task(batch, ["--title", "Add Rate Limiter"])
    assert (batch / "tasks" / "task-001-add-rate-limiter").is_dir()


def test_new_task_requires_a_slug(tmp_path, capsys):
    assert orch.cmd_new_task(tmp_path / "batch", []) == 2
    assert "slug is required" in capsys.readouterr().err


def test_new_task_does_not_clobber_existing(tmp_path, capsys):
    batch = tmp_path / "batch"
    existing = batch / "tasks" / "task-001-dup"
    existing.mkdir(parents=True)
    (existing / "input.md").write_text("MINE", encoding="utf-8")
    # next number is 002, so a fresh slug never collides; force a collision by
    # pre-creating the slot the next number would take.
    (batch / "tasks" / "task-002-dup").mkdir(parents=True)
    rc = orch.cmd_new_task(batch, ["dup"])  # would be task-003-dup → no clobber, succeeds
    assert rc == 0
    assert (batch / "tasks" / "task-003-dup").is_dir()
    assert (existing / "input.md").read_text(encoding="utf-8") == "MINE"  # untouched
