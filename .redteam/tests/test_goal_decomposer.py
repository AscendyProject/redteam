"""Slice B — goal decomposer + decomposition review invariants.

All tests stub the decomposer worker adapter (no live model call) and the
reviewer (review_with_fallback returns a deterministic decision). Asserts:

  (a) invalid generated manifest (cycle / self-ref / unknown ref / exceeds
      max_tasks) aborts the whole batch via _load_goal_manifest fail-closed
      path — no state.json seeded, no task run.
  (b) generated goal.json with a task that lacks a non-empty input.md aborts
      fail-closed BEFORE review is invoked (pre-approval completeness gate,
      PR-004). Covers missing and empty input.md subcases.
  (c) non-APPROVED decomposition review aborts fail-closed (no state.json
      seeded) and decompose_review.md is persisted.
  (d) cannot-decompose contract — DECOMPOSE_DECISION: CANNOT_DECOMPOSE marker +
      decompose_blocked.md → exit non-zero, reviewer not invoked; and exit 0 +
      no goal.json + no marker → fail closed under outcome (c).
  (e) APPROVED decomposition with a valid manifest lets the existing Slice A/C
      scheduler run the stack on a subsequent start/resume.
  (f) absent goal.md → decompose never invoked, process_batch / _run_pipeline
      behave byte-for-byte as under Slice A/C.
  (g) re-running decompose against a batch that already has goal.json OR any
      tasks/<id>/input.md fails closed (no writes/modifications).
  (h) same-provider worker/reviewer configuration refuses fail-closed via
      the existing _adversarial_pairing_error helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- helpers ----------


def _make_batch(
    tmp_path: Path,
    *,
    with_goal_md: bool = True,
    goal_md_text: str = "Goal: build a feature.",
) -> Path:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    (batch_dir / "tasks").mkdir()
    if with_goal_md:
        (batch_dir / "goal.md").write_text(goal_md_text, encoding="utf-8")
    return batch_dir


def _simple_goal_json(task_ids: list[str], max_tasks: int | None = None) -> str:
    tasks = {tid: {"depends_on": []} for tid in task_ids}
    data: dict = {"goal": "test", "tasks": tasks}
    if max_tasks is not None:
        data["ceilings"] = {"max_tasks": max_tasks}
    return json.dumps(data)


class _FakeWorker:
    """Stubs the goal-decomposer worker adapter.

    Writes goal.json and tasks/<id>/input.md to batch_dir when invoked,
    simulating the three outcomes the runner contract recognises.
    """

    def __init__(
        self,
        batch_dir: Path,
        *,
        task_ids: list[str] | None = None,
        write_briefs: bool = True,
        empty_brief_ids: list[str] | None = None,
        cannot_decompose: bool = False,
        exit_code: int = 0,
        goal_json_override: str | None = None,
    ) -> None:
        self._batch = batch_dir
        self._task_ids = task_ids or []
        self._write_briefs = write_briefs
        self._empty_brief_ids = set(empty_brief_ids or [])
        self._cannot_decompose = cannot_decompose
        self._exit_code = exit_code
        self._goal_json_override = goal_json_override

    def invoke(self, **_kwargs: object) -> dict:
        # Outcome (b): cannot-decompose
        if self._cannot_decompose:
            (self._batch / "decompose_blocked.md").write_text(
                "cannot decompose: multi-parent required", encoding="utf-8"
            )
            return {
                "returncode": 0,
                "stdout": "DECOMPOSE_DECISION: CANNOT_DECOMPOSE",
                "stderr": "",
            }

        # Non-zero exit → outcome (c) error
        if self._exit_code != 0:
            return {"returncode": self._exit_code, "stdout": "", "stderr": "worker error"}

        # Write goal.json
        goal_json_str = self._goal_json_override or _simple_goal_json(self._task_ids)
        (self._batch / "goal.json").write_text(goal_json_str, encoding="utf-8")

        # Create task directories and optionally write briefs
        for tid in self._task_ids:
            task_dir = self._batch / "tasks" / tid
            task_dir.mkdir(parents=True, exist_ok=True)
            if self._write_briefs:
                content = "" if tid in self._empty_brief_ids else f"Brief for {tid}."
                (task_dir / "input.md").write_text(content, encoding="utf-8")

        return {"returncode": 0, "stdout": "decompose done", "stderr": ""}


def _approved() -> dict:
    return {"decision": "APPROVED", "raw": "REVIEW_DECISION: APPROVED", "parse_status": "ok"}


def _rejected(decision: str = "CHANGES_REQUESTED") -> dict:
    return {
        "decision": decision,
        "raw": f"REVIEW_DECISION: {decision}",
        "parse_status": "ok",
    }


# ---------- (g) idempotency ----------


def test_decompose_fails_if_goal_json_exists(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    (batch_dir / "goal.json").write_text("{}", encoding="utf-8")

    rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


def test_decompose_fails_if_task_input_md_exists(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_dir = batch_dir / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("existing brief", encoding="utf-8")

    rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not (batch_dir / "goal.json").exists()


def test_decompose_idempotency_writes_nothing(tmp_path: Path) -> None:
    """When aborted for idempotency, no new file is created or modified."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    existing_json = batch_dir / "goal.json"
    existing_json.write_text('{"x": 1}', encoding="utf-8")
    mtime_before = existing_json.stat().st_mtime

    orch.cmd_decompose(batch_dir)

    assert existing_json.stat().st_mtime == mtime_before  # untouched


# ---------- (f) absent goal.md ----------


def test_decompose_requires_goal_md(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path, with_goal_md=False)

    rc = orch.cmd_decompose(batch_dir)

    assert rc != 0


def test_process_batch_unchanged_without_goal_json(tmp_path: Path) -> None:
    """Flat mode (no goal.json) is byte-for-byte unchanged from Slice A/C."""
    orch = _orch()
    batch_dir = tmp_path / "batch"
    (batch_dir / "tasks").mkdir(parents=True)

    results = orch.process_batch(batch_dir)

    assert results == {}


def test_run_pipeline_unchanged_when_no_goal_md(tmp_path: Path, monkeypatch) -> None:
    """_run_pipeline does not call the decomposer when goal.md is absent."""
    orch = _orch()
    batch_dir = tmp_path / "batch"
    (batch_dir / "tasks").mkdir(parents=True)
    decompose_called = []

    monkeypatch.setattr(
        orch,
        "cmd_decompose",
        lambda _bd: decompose_called.append(True) or 0,
    )

    orch._run_pipeline(batch_dir, label="start")

    assert not decompose_called


# ---------- (h) same-provider guard ----------


def test_decompose_refuses_same_provider_pairing(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)

    with (
        patch("redteam_orchestrator.reviewer_provider", return_value="claude"),
        patch("redteam_orchestrator.worker_provider", return_value="claude"),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    # no artifact written when guard fires
    assert not (batch_dir / "goal.json").exists()
    assert not (batch_dir / "decompose_review.md").exists()


# ---------- (d) cannot-decompose contract ----------


def test_cannot_decompose_exits_nonzero_no_reviewer(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    fake_worker = _FakeWorker(batch_dir, cannot_decompose=True)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()
    assert (batch_dir / "decompose_blocked.md").is_file()
    assert not (batch_dir / "goal.json").exists()
    assert not list(batch_dir.rglob("state.json"))


def test_worker_exit0_no_goal_json_no_marker_fails_closed(tmp_path: Path) -> None:
    """Outcome (c): exit 0 + no goal.json + no CANNOT_DECOMPOSE → fail closed."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)

    # Worker exits 0 but writes nothing (no goal.json, no marker, no blocked.md)
    fake_worker = MagicMock()
    fake_worker.invoke.return_value = {"returncode": 0, "stdout": "oops", "stderr": ""}

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()
    assert not list(batch_dir.rglob("state.json"))


def test_cannot_decompose_marker_without_blocked_md_fails_closed(tmp_path: Path) -> None:
    """Outcome (c): marker present but no decompose_blocked.md → not a valid cannot-decompose."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)

    # Worker emits marker but does NOT write decompose_blocked.md
    fake_worker = MagicMock()
    fake_worker.invoke.return_value = {
        "returncode": 0,
        "stdout": "DECOMPOSE_DECISION: CANNOT_DECOMPOSE",
        "stderr": "",
    }

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()


def test_cannot_decompose_with_stray_briefs_fails_closed(tmp_path: Path) -> None:
    """Outcome (c): marker + blocked.md + stray tasks/*/input.md → partial write, fail closed.

    A worker that emits CANNOT_DECOMPOSE but also wrote task briefs violates the
    contract (cannot-decompose requires NO goal.json / tasks/<id>/input.md / state.json
    writes). The runner must classify this as outcome (c), not (b), so the operator
    can inspect the inconsistent batch dir rather than silently accepting it.
    """
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)

    def stray_worker_invoke(**_kwargs: object) -> dict:
        # Writes decompose_blocked.md AND a stray task brief (contract violation)
        (batch_dir / "decompose_blocked.md").write_text("blocked", encoding="utf-8")
        stray_dir = batch_dir / "tasks" / "task-stray"
        stray_dir.mkdir(parents=True, exist_ok=True)
        (stray_dir / "input.md").write_text("stray brief", encoding="utf-8")
        return {
            "returncode": 0,
            "stdout": "DECOMPOSE_DECISION: CANNOT_DECOMPOSE",
            "stderr": "",
        }

    fake_worker = MagicMock()
    fake_worker.invoke.side_effect = stray_worker_invoke

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    # Must be outcome (c), not (b)
    assert result["status"] == "error"
    assert "cannot_decompose" not in result["status"]


def test_cannot_decompose_with_stray_state_fails_closed(tmp_path: Path) -> None:
    """Outcome (c): marker + blocked.md + stray tasks/*/state.json → partial write, fail closed (IR-005).

    A worker that emits CANNOT_DECOMPOSE but also wrote a tasks/<id>/state.json is the
    worst partial-write: with no goal.json the scheduler runs flat mode and
    _run_one_task executes a pre-seeded task dir WITHOUT re-seeding, bypassing the
    no_input_md guard, so attacker-controlled state would run. The runner must classify
    this as outcome (c), not (b) — the per-task state-machine surface stays untouched
    until APPROVED.
    """
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)

    def stray_worker_invoke(**_kwargs: object) -> dict:
        # Writes decompose_blocked.md AND a stray task state.json (contract violation)
        (batch_dir / "decompose_blocked.md").write_text("blocked", encoding="utf-8")
        stray_dir = batch_dir / "tasks" / "task-stray"
        stray_dir.mkdir(parents=True, exist_ok=True)
        (stray_dir / "state.json").write_text('{"task_id": "task-stray"}', encoding="utf-8")
        return {
            "returncode": 0,
            "stdout": "DECOMPOSE_DECISION: CANNOT_DECOMPOSE",
            "stderr": "",
        }

    fake_worker = MagicMock()
    fake_worker.invoke.side_effect = stray_worker_invoke

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    # Must be outcome (c), not (b)
    assert result["status"] == "error"
    assert "cannot_decompose" not in result["status"]


# ---------- runner contract enforced in decompose.run() directly (IR-001) ----------


def _decompose_mod():
    _orch()  # ensures workflows/ is on sys.path via _engine
    import phase_runners.decompose as m

    return m


def test_run_partial_write_returns_error(tmp_path: Path) -> None:
    """run() itself fails closed on goal.json + an empty brief (not deferred to caller)."""
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)
    fake_worker = _FakeWorker(batch_dir, task_ids=["task-001", "task-002"], empty_brief_ids=["task-002"])

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    assert result["status"] == "error"
    assert "task-002" in result["message"]


def test_run_missing_brief_returns_error(tmp_path: Path) -> None:
    """run() fails closed when a manifest task has no input.md at all."""
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)
    fake_worker = _FakeWorker(batch_dir, task_ids=["task-001"], write_briefs=False)

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    assert result["status"] == "error"
    assert "task-001" in result["message"]


def test_run_unparseable_goal_json_returns_error(tmp_path: Path) -> None:
    """run() fails closed when the worker writes an unparseable goal.json."""
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)
    fake_worker = _FakeWorker(batch_dir, task_ids=["task-001"], goal_json_override="{not valid json")

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    assert result["status"] == "error"
    assert "unparseable" in result["message"]


def test_run_complete_returns_success(tmp_path: Path) -> None:
    """run() returns success only when goal.json + every brief is present and non-empty."""
    decompose_mod = _decompose_mod()
    batch_dir = _make_batch(tmp_path)
    fake_worker = _FakeWorker(batch_dir, task_ids=["task-001", "task-002"])

    with patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker):
        result = decompose_mod.run(batch_dir, {})

    assert result["status"] == "success"


# ---------- (c) non-APPROVED review ----------


def test_rejected_review_no_state_seeded(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_rejected()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert (batch_dir / "decompose_review.md").is_file()
    assert not list(batch_dir.rglob("state.json"))


def test_rescue_required_review_no_state_seeded(tmp_path: Path) -> None:
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch(
            "redteam_orchestrator.review_with_fallback",
            return_value=_rejected("RESCUE_REQUIRED"),
        ),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert (batch_dir / "decompose_review.md").is_file()
    assert not list(batch_dir.rglob("state.json"))


# ---------- (b) brief completeness gate (PR-004) ----------


def test_missing_brief_aborts_before_review(tmp_path: Path) -> None:
    """goal.json lists a task that has no input.md — aborts before reviewer is called."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    # Worker writes goal.json but NOT the input.md
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, write_briefs=False)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()
    assert not list(batch_dir.rglob("state.json"))


def test_empty_brief_aborts_before_review(tmp_path: Path) -> None:
    """goal.json task with an empty input.md aborts before reviewer is called."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, empty_brief_ids=["task-001"])

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()
    assert not list(batch_dir.rglob("state.json"))


def test_multi_task_partial_briefs_aborts(tmp_path: Path) -> None:
    """When only some tasks have briefs, the completeness gate aborts."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001", "task-002"]
    # task-002 has no brief
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, empty_brief_ids=["task-002"])

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback") as mock_review,
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    mock_review.assert_not_called()


# ---------- (a) invalid manifest aborts via _load_goal_manifest ----------


def test_cycle_in_manifest_aborts_via_load_goal_manifest(tmp_path: Path) -> None:
    """Cycle A→B→A: _load_goal_manifest detects it, cmd_decompose exits non-zero."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001", "task-002"]

    cycle_manifest = json.dumps(
        {
            "goal": "test",
            "tasks": {
                "task-001": {"depends_on": ["task-002"]},
                "task-002": {"depends_on": ["task-001"]},
            },
        }
    )
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, goal_json_override=cycle_manifest)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


def test_exceeds_max_tasks_aborts_via_load_goal_manifest(tmp_path: Path) -> None:
    """max_tasks=2 but 3 tasks generated: _load_goal_manifest enforces ceiling."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001", "task-002", "task-003"]

    manifest = _simple_goal_json(task_ids, max_tasks=2)
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, goal_json_override=manifest)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


def test_self_ref_in_manifest_aborts_via_load_goal_manifest(tmp_path: Path) -> None:
    """Self-dependency: _load_goal_manifest detects and aborts."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]

    self_ref = json.dumps({"goal": "test", "tasks": {"task-001": {"depends_on": ["task-001"]}}})
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, goal_json_override=self_ref)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


def test_unknown_ref_in_manifest_aborts_via_load_goal_manifest(tmp_path: Path) -> None:
    """Unknown parent reference: _load_goal_manifest detects and aborts."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]

    unknown_ref = json.dumps({"goal": "test", "tasks": {"task-001": {"depends_on": ["task-does-not-exist"]}}})
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, goal_json_override=unknown_ref)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


def test_multi_parent_in_manifest_aborts_via_load_goal_manifest(tmp_path: Path) -> None:
    """Two-parent depends_on: _load_goal_manifest rejects (single-parent v1)."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001", "task-002", "task-003"]

    multi_parent = json.dumps(
        {
            "goal": "test",
            "tasks": {
                "task-001": {"depends_on": []},
                "task-002": {"depends_on": []},
                "task-003": {"depends_on": ["task-001", "task-002"]},
            },
        }
    )
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids, goal_json_override=multi_parent)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc != 0
    assert not list(batch_dir.rglob("state.json"))


# ---------- (e) APPROVED lets scheduler run ----------


def test_approved_decompose_exits_zero(tmp_path: Path) -> None:
    """APPROVED review + valid manifest + all briefs → exit 0."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc == 0
    assert (batch_dir / "goal.json").is_file()
    assert (batch_dir / "decompose_review.md").is_file()
    # decompose does NOT seed task state
    assert not list(batch_dir.rglob("state.json"))


def test_approved_decompose_manifest_loadable_by_scheduler(tmp_path: Path) -> None:
    """After APPROVED decompose, _load_goal_manifest succeeds and scheduler picks it up."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc == 0

    # The generated manifest is valid per _load_goal_manifest (same Slice A+C path)
    manifest = orch._load_goal_manifest(batch_dir, batch_dir / "goal.json")
    assert manifest["goal"] == "test"
    assert "task-001" in manifest["deps"]


def test_approved_decompose_scheduler_runs_tasks(tmp_path: Path, monkeypatch) -> None:
    """After APPROVED decompose, process_batch with the generated goal.json runs the
    scheduler correctly (process_task and _seed_state stubbed to avoid git calls)."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path)
    task_ids = ["task-001"]
    fake_worker = _FakeWorker(batch_dir, task_ids=task_ids)

    with (
        patch("phase_runners.decompose.get_worker_adapter", return_value=fake_worker),
        patch("redteam_orchestrator.review_with_fallback", return_value=_approved()),
    ):
        rc = orch.cmd_decompose(batch_dir)

    assert rc == 0

    # Stub process_task so process_batch can run without git
    ran: list[str] = []

    def fake_seed(td: Path) -> None:
        pass

    def fake_task(td: Path, *, resolved_base: str | None = None, base_is_parent: bool = False) -> str:
        ran.append(td.name)
        return "done"

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert "task-001" in ran
    assert results["task-001"] == "done"
