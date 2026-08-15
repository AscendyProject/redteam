"""Tests for per-phase telemetry capture (Phase 0, task-001-phase-telemetry).

Covers:
  - ClaudeWorkerAdapter.invoke populates cost_usd/duration_sec/model/provider.
  - Runner materialization: plan_outcome and create_pr carry telemetry on PhaseResult.
  - Orchestrator appends exactly one phase_telemetry entry per worker-invoking phase.
  - Codex-worker path: cost_usd/duration_sec/model are None, provider == "codex".
  - Missing-signal path (parsed_json=None): None fields, outcome reflects actual status.
  - create_pr telemetry entry (both approved and error paths).
  - The orchestrator appends an entry only when a runner returns `provider`.
  - rescue emits no telemetry: it invokes no model, it only validates a report.
  - plan_review / review_code DO emit (#172) — they invoke a reviewer model — with
    cost/model/duration null, since a reviewer transport reports none of them.
  - Non-mutation guarantee: telemetry capture never changes PhaseResult.status.
  - Legacy state.json without phase_telemetry key: setdefault creates the list.
  - State template: phase_telemetry key is an empty list.
  - No-secret-bleed shape: entry keys are exactly the six allowed ones.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / ".redteam" / "templates" / "state.template.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_ENTRY_KEYS = frozenset({"phase", "provider", "model", "cost_usd", "duration_sec", "outcome"})
_FORBIDDEN_ENTRY_KEYS = frozenset({"feedback", "log", "diff", "stderr", "stdout", "last_failure_log"})


def _make_parsed_json(
    *,
    total_cost_usd: float | None = 1.23,
    duration_ms: int | None = 4500,
) -> dict:
    """Build a fake `result` event dict as produced by run_claude stream-json.

    Deliberately carries NO `model` key (#168): the real CLI reports the model on
    the `system`/`init` event, not on `result`. The previous version of this
    helper fabricated one here, which is why the adapter's `parsed_json["model"]`
    read passed its test while returning None in production. run_claude surfaces
    the real value as ClaudeRunResult["init_model"].
    """
    d: dict[str, Any] = {"type": "result", "is_error": False}
    if total_cost_usd is not None:
        d["total_cost_usd"] = total_cost_usd
    if duration_ms is not None:
        d["duration_ms"] = duration_ms
    return d


def _engine_base():
    import _engine

    return _engine.base()


def _load_adapter_modules():
    import adapters as _adapters_pkg
    import adapters.claude as _claude_mod

    return _adapters_pkg, _claude_mod


def _load_orchestrator():
    import _engine

    return _engine.orchestrator()


def _load_plan_outcome():
    import phase_runners.plan_outcome as m

    return m


def _load_create_pr():
    import _engine

    return _engine.create_pr()


def _load_base():
    import _engine

    return _engine.base()


# ---------------------------------------------------------------------------
# 1. ClaudeWorkerAdapter.invoke — happy path
# ---------------------------------------------------------------------------


def test_claude_adapter_invoke_populates_telemetry(monkeypatch):
    """ClaudeWorkerAdapter.invoke returns WorkerRunResult with cost_usd,
    duration_sec (= duration_ms / 1000), model, and provider == 'claude'."""
    _adapters_pkg, _claude_mod = _load_adapter_modules()
    parsed = _make_parsed_json(total_cost_usd=2.50, duration_ms=3000)

    fake_run_result = {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "parsed_json": parsed,
        "init_model": "claude-opus-4-7",  # #168: from the init event, not result
    }
    monkeypatch.setattr(_claude_mod, "run_claude", lambda **kw: fake_run_result)
    monkeypatch.setattr(_claude_mod, "claude_model_for_role", lambda state, role: None)

    adapter = _claude_mod.ClaudeWorkerAdapter(state={})
    result = adapter.invoke(role="implementer", agent="implementer", prompt="go", cwd=_REPO_ROOT)

    assert result["provider"] == "claude"
    assert result["cost_usd"] == 2.50
    assert result["duration_sec"] == 3.0  # 3000 / 1000
    assert result["model"] == "claude-opus-4-7"
    assert result["returncode"] == 0


def test_claude_adapter_invoke_none_parsed_json(monkeypatch):
    """When parsed_json is None (timeout / transport error), the adapter returns only
    the three base fields (returncode/stdout/stderr). Telemetry fields (cost_usd,
    duration_sec, model) are absent and resolve to None via .get(). The provider is
    set by the runner's materialization rule via worker_provider(state), not here."""
    _adapters_pkg, _claude_mod = _load_adapter_modules()
    fake_run_result = {
        "returncode": 124,
        "stdout": "",
        "stderr": "timeout",
        "parsed_json": None,
    }
    monkeypatch.setattr(_claude_mod, "run_claude", lambda **kw: fake_run_result)
    monkeypatch.setattr(_claude_mod, "claude_model_for_role", lambda state, role: None)

    adapter = _claude_mod.ClaudeWorkerAdapter(state={})
    result = adapter.invoke(role="implementer", agent="implementer", prompt="go", cwd=_REPO_ROOT)

    assert result["returncode"] == 124
    # Telemetry fields are absent on the None-parsed_json path; .get() returns None
    assert result.get("cost_usd") is None
    assert result.get("duration_sec") is None
    assert result.get("model") is None
    # provider is intentionally absent here; the runner sets it via worker_provider(state)
    assert "provider" not in result


def test_claude_adapter_missing_keys_in_parsed_json(monkeypatch):
    """Keys absent from parsed_json degrade to None gracefully."""
    _adapters_pkg, _claude_mod = _load_adapter_modules()
    # parsed_json with no telemetry keys
    fake_run_result = {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "parsed_json": {"type": "result", "is_error": False},
    }
    monkeypatch.setattr(_claude_mod, "run_claude", lambda **kw: fake_run_result)
    monkeypatch.setattr(_claude_mod, "claude_model_for_role", lambda state, role: None)

    adapter = _claude_mod.ClaudeWorkerAdapter(state={})
    result = adapter.invoke(role="implementer", agent="implementer", prompt="go", cwd=_REPO_ROOT)

    assert result["provider"] == "claude"
    assert result["cost_usd"] is None
    assert result["duration_sec"] is None
    assert result["model"] is None


# ---------------------------------------------------------------------------
# 2. Runner materialization — plan_outcome
# ---------------------------------------------------------------------------


def test_plan_outcome_approved_carries_telemetry(monkeypatch, tmp_path):
    """plan_outcome.run returns a PhaseResult carrying all four telemetry fields
    on the approved path."""
    m = _load_plan_outcome()

    fake_wr = {
        "returncode": 0,
        "stdout": "done",
        "stderr": "",
        "parsed_json": _make_parsed_json(total_cost_usd=0.5, duration_ms=2000),
        "cost_usd": 0.5,
        "duration_sec": 2.0,
        "model": "claude-test",
        "provider": "claude",
    }

    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    (task_dir / "outcome.md").write_text("# Outcome\n", encoding="utf-8")
    (task_dir / "input.md").write_text("do x", encoding="utf-8")

    monkeypatch.setattr(m, "get_worker_adapter", lambda state: _make_adapter(fake_wr))
    monkeypatch.setattr(m, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(m, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(m, "project_config", lambda: _fake_proj())
    monkeypatch.setattr(m, "compute_repo_diff", lambda cwd=None: "")

    state: dict = {"mode": "agent-pair", "models": {"implementer": "claude-sonnet-4-6"}}
    result = m.run(task_dir, state)

    assert result["status"] == "approved"
    assert result.get("provider") == "claude"
    assert result.get("cost_usd") == 0.5
    assert result.get("duration_sec") == 2.0
    assert result.get("model") == "claude-test"


def test_plan_outcome_error_carries_telemetry(monkeypatch, tmp_path):
    """plan_outcome.run returns a PhaseResult carrying telemetry on the error path too."""
    m = _load_plan_outcome()

    fake_wr = {
        "returncode": 1,
        "stdout": "",
        "stderr": "fail",
        "parsed_json": None,
        "cost_usd": None,
        "duration_sec": None,
        "model": None,
        "provider": "claude",
    }

    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    # Do NOT create outcome.md → runner will take the error path.

    monkeypatch.setattr(m, "get_worker_adapter", lambda state: _make_adapter(fake_wr))
    monkeypatch.setattr(m, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(m, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(m, "project_config", lambda: _fake_proj())
    monkeypatch.setattr(m, "compute_repo_diff", lambda cwd=None: "")

    state: dict = {"mode": "agent-pair", "models": {"implementer": "claude-sonnet-4-6"}}
    result = m.run(task_dir, state)

    assert result["status"] == "error"
    assert result.get("provider") == "claude"
    assert result.get("cost_usd") is None
    assert result.get("duration_sec") is None


# ---------------------------------------------------------------------------
# 3. Orchestrator append — Claude happy path
# ---------------------------------------------------------------------------


def test_orchestrator_appends_telemetry_entry_claude(monkeypatch, tmp_path):
    """After a worker-invoking runner returns a PhaseResult with a Claude provider,
    state['phase_telemetry'] gains exactly one entry with the correct fields."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome")

    def fake_plan_outcome(td, st):
        return {
            "status": "approved",
            "feedback": "",
            "log": "",
            "diff": "",
            "provider": "claude",
            "cost_usd": 1.23,
            "duration_sec": 4.5,
            "model": "claude-opus-4-7",
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_plan_outcome)

    orch.process_task(task_dir)

    # Reload state from disk
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    assert entry["phase"] == "plan_outcome"
    assert entry["provider"] == "claude"
    assert entry["cost_usd"] == 1.23
    assert entry["duration_sec"] == 4.5
    assert entry["model"] == "claude-opus-4-7"
    assert entry["outcome"] == "approved"


# ---------------------------------------------------------------------------
# 4. Codex-worker path
# ---------------------------------------------------------------------------


def test_orchestrator_appends_telemetry_codex_path(monkeypatch, tmp_path):
    """With the worker resolved to codex, the appended entry has provider='codex'
    and cost_usd/duration_sec/model all None. No number is fabricated."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome")

    def fake_plan_outcome(td, st):
        return {
            "status": "approved",
            "feedback": "",
            "log": "",
            "diff": "",
            "provider": "codex",
            "cost_usd": None,
            "duration_sec": None,
            "model": None,
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_plan_outcome)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    assert entry["provider"] == "codex"
    assert entry["cost_usd"] is None
    assert entry["duration_sec"] is None
    assert entry["model"] is None
    assert entry["outcome"] == "approved"


# ---------------------------------------------------------------------------
# 5. Missing-signal path (parsed_json=None)
# ---------------------------------------------------------------------------


def test_orchestrator_appends_telemetry_missing_signal(monkeypatch, tmp_path):
    """When the runner returns with parsed_json=None (timeout/transport error),
    the entry still appends with None numerics and outcome reflects actual status.
    max_retries=0 so the error causes an immediate defer after one attempt (avoiding
    multiple retry iterations that would append more than one telemetry entry)."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome", max_retries=0)

    def fake_plan_outcome(td, st):
        return {
            "status": "error",
            "feedback": "timeout",
            "log": "timeout",
            "diff": "",
            "provider": "claude",
            "cost_usd": None,
            "duration_sec": None,
            "model": None,
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_plan_outcome)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    assert entry["provider"] == "claude"
    assert entry["cost_usd"] is None
    assert entry["duration_sec"] is None
    assert entry["outcome"] == "error"


# ---------------------------------------------------------------------------
# 6. create_pr entry (settles PR-001)
# ---------------------------------------------------------------------------


def test_orchestrator_appends_telemetry_create_pr_approved(monkeypatch, tmp_path):
    """After create_pr.run returns approved from its pr-author invoke call,
    exactly one phase_telemetry entry with phase='create_pr' is appended."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="create_pr")

    def fake_create_pr(td, st):
        return {
            "status": "approved",
            "feedback": "",
            "log": "",
            "diff": "",
            "provider": "claude",
            "cost_usd": 0.1,
            "duration_sec": 1.0,
            "model": "claude-sonnet-4-6",
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "create_pr", fake_create_pr)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    assert entry["phase"] == "create_pr"
    assert entry["provider"] == "claude"
    assert entry["outcome"] == "approved"


def test_orchestrator_appends_telemetry_create_pr_error(monkeypatch, tmp_path):
    """After create_pr.run returns error from its pr-author invoke call,
    an entry is still appended with outcome='error'.
    max_retries=0 so the error causes an immediate defer after one attempt."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="create_pr", max_retries=0)

    def fake_create_pr(td, st):
        return {
            "status": "error",
            "feedback": "pr failed",
            "log": "pr failed",
            "diff": "",
            "provider": "claude",
            "cost_usd": None,
            "duration_sec": None,
            "model": None,
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "create_pr", fake_create_pr)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    assert entry["phase"] == "create_pr"
    assert entry["outcome"] == "error"


# ---------------------------------------------------------------------------
# 7. Reviewer-transport phases do NOT append telemetry
# ---------------------------------------------------------------------------


def test_runner_without_provider_appends_no_telemetry_plan_review(monkeypatch, tmp_path):
    """Sentinel behaviour: a runner returning no `provider` produces no entry.

    Drives a FAKE runner — this pins the orchestrator's sentinel rule, not the
    real plan_review, which does emit telemetry since #172 (see
    test_real_plan_review_emits_telemetry below)."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_review")

    def fake_plan_review(td, st):
        # No 'provider' field — reviewer-transport phase
        return {"status": "approved", "feedback": "", "log": "", "diff": ""}

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_review", fake_plan_review)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert telemetry == [], f"a runner without `provider` must append nothing; got {telemetry}"


def test_runner_without_provider_appends_no_telemetry_review_code(monkeypatch, tmp_path):
    """Sentinel behaviour: a runner returning no `provider` produces no entry.

    Drives a FAKE runner — the real review_code emits telemetry since #172 (see
    test_real_review_code_emits_telemetry below)."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="review_code")

    def fake_review_code(td, st):
        return {"status": "approved", "feedback": "", "log": "", "diff": ""}

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "review_code", fake_review_code)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert telemetry == [], f"a runner without `provider` must append nothing; got {telemetry}"


def test_rescue_does_not_append_telemetry(monkeypatch, tmp_path):
    """rescue emits no telemetry, and correctly so: it invokes no model at all —
    it validates a manually produced rescue_report.md. Unchanged by #172."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="rescue")

    def fake_rescue(td, st):
        return {"status": "approved", "feedback": "", "log": "", "diff": ""}

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "rescue", fake_rescue)

    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert telemetry == [], f"rescue should not append telemetry; got {telemetry}"


# ---------------------------------------------------------------------------
# 8. Non-mutation guarantee
# ---------------------------------------------------------------------------


def test_telemetry_does_not_change_phase_result_status(monkeypatch, tmp_path):
    """An approved result stays approved; an error result stays error.
    Telemetry capture never rewrites PhaseResult.status.
    The error sub-case uses max_retries=0 to stop after one attempt (avoiding
    the retry loop that would append multiple telemetry entries)."""
    orch = _load_orchestrator()

    for status in ("approved", "error"):
        task_dir = _setup_task_dir(tmp_path / status)
        # max_retries=0 for the error case: an always-erroring runner with retries
        # allowed would loop (appending multiple entries) before deferring.
        _setup_state(task_dir, next_phase="plan_outcome", max_retries=0 if status == "error" else 2)

        def fake_runner(td, st, _s=status):
            return {
                "status": _s,
                "feedback": "",
                "log": "",
                "diff": "",
                "provider": "claude",
                "cost_usd": 1.0,
                "duration_sec": 1.0,
                "model": "m",
            }

        _patch_orchestrator_for_single_phase(
            monkeypatch, orch, tmp_path / status, task_dir, "plan_outcome", fake_runner
        )
        orch.process_task(task_dir)

        saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        telemetry = saved.get("phase_telemetry", [])
        assert len(telemetry) == 1
        assert telemetry[0]["outcome"] == status, f"expected outcome=={status!r}, got {telemetry[0]['outcome']!r}"


def test_telemetry_append_error_swallowed_to_stderr(monkeypatch, tmp_path, capsys):
    """When the telemetry append itself raises (patched to raise RuntimeError),
    the exception is swallowed to stderr and the phase's dispatch branch is unchanged."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome")

    def fake_runner(td, st):
        return {
            "status": "approved",
            "feedback": "",
            "log": "",
            "diff": "",
            "provider": "claude",
            "cost_usd": 1.0,
            "duration_sec": 1.0,
            "model": "m",
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_runner)

    # Patch build_telemetry_entry to raise
    monkeypatch.setattr(
        orch, "build_telemetry_entry", lambda phase, result: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    orch.process_task(task_dir)

    # Dispatch must still proceed (approved → next phase)
    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    # phase_telemetry was never written (build raised before append)
    # outcome (next_phase) must NOT be "error" or "deferred" due to telemetry
    assert saved.get("next_phase") != "deferred"

    captured = capsys.readouterr()
    assert "telemetry" in captured.err.lower() or "boom" in captured.err


# ---------------------------------------------------------------------------
# 9. Legacy state.json without phase_telemetry key
# ---------------------------------------------------------------------------


def test_legacy_state_missing_key_still_appends(monkeypatch, tmp_path):
    """A state dict that lacks 'phase_telemetry' gets setdefault([]) and the
    new entry is appended without KeyError."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome")

    # Confirm the on-disk state has NO phase_telemetry key
    raw = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    raw.pop("phase_telemetry", None)
    (task_dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")

    def fake_runner(td, st):
        return {
            "status": "approved",
            "feedback": "",
            "log": "",
            "diff": "",
            "provider": "claude",
            "cost_usd": 0.5,
            "duration_sec": 2.0,
            "model": "m",
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_runner)
    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    assert telemetry[0]["provider"] == "claude"


# ---------------------------------------------------------------------------
# 10. State template shape
# ---------------------------------------------------------------------------


def test_state_template_has_phase_telemetry():
    """state.template.json parses as JSON and contains 'phase_telemetry' as an
    empty list at the top level."""
    assert _TEMPLATE_PATH.exists(), f"state.template.json not found at {_TEMPLATE_PATH}"
    data = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "phase_telemetry" in data, "state.template.json is missing 'phase_telemetry' key"
    assert data["phase_telemetry"] == [], (
        f"state.template.json 'phase_telemetry' should be an empty list, got {data['phase_telemetry']!r}"
    )


# ---------------------------------------------------------------------------
# 11. No-secret-bleed shape
# ---------------------------------------------------------------------------


def test_telemetry_entry_has_exactly_six_allowed_keys(monkeypatch, tmp_path):
    """The appended entry's keys are exactly {phase, provider, model, cost_usd,
    duration_sec, outcome} — no free-text field (feedback, log, diff, stderr,
    stdout, last_failure_log, review text) leaks in."""
    orch = _load_orchestrator()
    task_dir = _setup_task_dir(tmp_path)
    _setup_state(task_dir, next_phase="plan_outcome")

    def fake_runner(td, st):
        return {
            "status": "approved",
            "feedback": "SENSITIVE FEEDBACK",
            "log": "SENSITIVE LOG",
            "diff": "SENSITIVE DIFF",
            "provider": "claude",
            "cost_usd": 1.0,
            "duration_sec": 2.0,
            "model": "claude-test",
        }

    _patch_orchestrator_for_single_phase(monkeypatch, orch, tmp_path, task_dir, "plan_outcome", fake_runner)
    orch.process_task(task_dir)

    saved = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    telemetry = saved.get("phase_telemetry", [])
    assert len(telemetry) == 1
    entry = telemetry[0]
    entry_keys = frozenset(entry.keys())
    assert entry_keys == _EXPECTED_ENTRY_KEYS, f"entry keys mismatch. expected {_EXPECTED_ENTRY_KEYS}, got {entry_keys}"
    for forbidden in _FORBIDDEN_ENTRY_KEYS:
        assert forbidden not in entry, f"forbidden key {forbidden!r} leaked into telemetry entry"


# ---------------------------------------------------------------------------
# Helpers for orchestrator tests
# ---------------------------------------------------------------------------


def _fake_proj():
    """Minimal fake project config."""
    proj = MagicMock()
    proj.context_file = str(_REPO_ROOT / ".redteam" / "docs" / "project-context.md")
    proj.source_dirs = [".redteam/workflows/"]
    proj.test_dir = ".redteam/tests/"
    proj.test_file_glob = "test_*.py"
    proj.verify_command = "bash .redteam/scripts/verify.sh"
    proj.verification_allowlist = ["bash", "pytest", "ruff"]
    proj.branch_prefix = "redteam"
    proj.base_branch = "main"
    return proj


def _make_adapter(fake_result: dict):
    """Return a fake WorkerAdapter whose invoke() returns fake_result."""

    class _FakeAdapter:
        name = "fake"

        def invoke(self, *, role, agent, prompt, cwd):
            return fake_result

    return _FakeAdapter()


def _setup_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task directory with input.md."""
    task_dir = tmp_path / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("do a thing", encoding="utf-8")
    return task_dir


def _setup_state(task_dir: Path, *, next_phase: str, max_retries: int = 2) -> dict:
    """Seed a minimal state.json and return the state dict.

    max_retries: set to 0 when the fake runner always returns an error and the test
    needs exactly one telemetry entry — max_retries=0 causes an immediate defer after
    the first failure instead of looping through retries (which would append multiple
    entries and make `len(telemetry) == 1` assertions fail).
    """
    state = {
        "task_id": task_dir.name,
        "mode": "agent-pair",
        "phase": next_phase,
        "next_phase": next_phase,
        "base_branch": "main",
        "phases_completed": [],
        "retries": {},
        "max_retries_per_phase": max_retries,
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "phase_telemetry": [],
        "verification": {},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def _patch_orchestrator_for_single_phase(
    monkeypatch,
    orch,
    tmp_path: Path,
    task_dir: Path,
    phase_name: str,
    fake_runner,
) -> None:
    """Patch orchestrator dependencies so process_task runs exactly one phase."""
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(orch, "_ensure_task_branch", lambda *a, **kw: f"redteam/{task_dir.name}")
    monkeypatch.setattr(orch, "load_config", lambda *a, **kw: _fake_config())
    monkeypatch.setattr(orch, "_adversarial_pairing_error", lambda state: None)

    # Run EXACTLY the one target phase: force the phase-advance to terminate the
    # loop after it, so process_task doesn't walk the rest of PHASE_ORDER (an
    # "approved" stub advances rather than terminates, which otherwise loops).
    monkeypatch.setattr(orch, "_next_phase", lambda state, current: "done")

    # Install fake runner; all other runners terminate the loop (approved, next done)
    def _terminating_runner(td, st):
        return {"status": "approved", "feedback": "", "log": "", "diff": ""}

    # Build a copy of PHASE_RUNNERS with the fake for the target phase and
    # terminating dummies for everything else (so loop stops after one real phase).
    fake_runners = {}
    for k in orch.PHASE_RUNNERS:
        if k == phase_name:
            fake_runners[k] = fake_runner
        else:
            fake_runners[k] = _terminating_runner
    monkeypatch.setattr(orch, "PHASE_RUNNERS", fake_runners)


def _fake_config():
    """Minimal fake config object."""
    cfg = MagicMock()
    cfg.tiers = {}
    cfg.project.base_branch = "main"
    cfg.project.branch_prefix = "redteam"
    cfg.project.verify_command = "bash .redteam/scripts/verify.sh"
    cfg.project.verification_allowlist = ["bash", "pytest", "ruff"]
    cfg.models.review_stages = None
    return cfg


# ---------------------------------------------------------------------------
# 8. #172 — the REAL reviewer runners emit telemetry
# ---------------------------------------------------------------------------
#
# The sentinel tests above drive fake runners, so they cannot see what the real
# ones do. These drive review_code.run / plan_review.run directly. Without them
# nothing would notice a regression back to "no reviewer telemetry", which is
# what made benchmark review_rounds read a real review loop as zero.


_PROJ_STUB = SimpleNamespace(security_checklist="sec.md", context_file="ctx.md")


def _ok_review(decision: str) -> dict:
    return {"decision": decision, "raw": f"REVIEW_DECISION: {decision}\n", "parse_status": "ok"}


def test_real_review_code_emits_telemetry(tmp_path):
    """#172: review_code.run sets `provider`, so the orchestrator records the round.

    Fails against pre-change code, where the runner set no telemetry fields and
    the entry was never appended.
    """
    from unittest.mock import MagicMock, patch

    import phase_runners.review_code as review_code

    state = {
        "mode": "agent-pair",
        "base_branch": "main",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "review_items": [],
    }
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=_ok_review("CHANGES_REQUESTED")),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value="deadbeef"),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
    ):
        result = review_code.run(tmp_path, state)

    assert result["status"] == "changes_requested"
    assert result["provider"] == "codex"
    # Never invented: a reviewer transport reports none of these.
    assert result["cost_usd"] is None
    assert result["model"] is None
    assert result["duration_sec"] is None

    # End to end: the entry the orchestrator would build is countable as a round.
    _base = _engine_base()
    entry = _base.build_telemetry_entry("review_code", result)
    assert entry["phase"] == "review_code"
    assert entry["provider"] == "codex"
    assert entry["outcome"] == "changes_requested"


def test_real_plan_review_emits_telemetry(tmp_path):
    """#172: plan_review.run sets `provider` on the headless path too."""
    from unittest.mock import MagicMock, patch

    import phase_runners.plan_review as plan_review

    (tmp_path / "outcome.md").write_text("x\n", encoding="utf-8")
    state = {"mode": "agent-pair", "models": {"reviewer": "codex"}}
    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.plan_review.review_with_fallback", return_value=_ok_review("APPROVED")),
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        result = plan_review.run(tmp_path, state)

    assert result["status"] == "approved"
    assert result["provider"] == "codex"
    assert result["cost_usd"] is None


def test_plan_review_records_headless_but_not_manual(tmp_path):
    """The sentinel keeps meaning "a model ran", not "the phase ran".

    Asserted as a contrast in one test on purpose. The manual half alone is
    unchanged behaviour and would pass against pre-change code, so it cannot
    stand as its own regression test; paired with the headless half — which is new
    — the whole discriminates while still pinning that the manual path must NOT
    start emitting.
    """
    from unittest.mock import MagicMock, patch

    import phase_runners.plan_review as plan_review

    (tmp_path / "outcome.md").write_text("x\n", encoding="utf-8")

    # Headless: a reviewer model runs → recorded (new behaviour).
    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.plan_review.review_with_fallback", return_value=_ok_review("APPROVED")),
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        headless = plan_review.run(tmp_path, {"mode": "agent-pair", "models": {"reviewer": "codex"}})

    # Manual: no adapter, no model runs → still nothing, so the exemption holds.
    (tmp_path / "plan_review.md").write_text("REVIEW_DECISION: APPROVED\n", encoding="utf-8")
    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=None),
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        manual = plan_review.run(tmp_path, {"mode": "agent-pair", "models": {}})

    assert headless["provider"] == "codex"
    assert "provider" not in manual


def test_tdd_review_code_records_worker_telemetry(tmp_path):
    """#172: the tdd branch invokes the WORKER adapter, so real cost/duration/model
    are recorded there — that path emitted nothing before this change."""
    from unittest.mock import patch

    import phase_runners.review_code as review_code

    (tmp_path / "code_review.md").write_text("REVIEW_DECISION: APPROVED\n", encoding="utf-8")
    worker = type(
        "W",
        (),
        {
            "invoke": lambda self, **kw: {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "cost_usd": 0.42,
                "duration_sec": 12.5,
                "model": "claude-sonnet-4-6",
                "provider": "claude",
            }
        },
    )()
    with (
        patch("phase_runners.review_code.get_worker_adapter", return_value=worker),
        patch("phase_runners.review_code.worker_provider", return_value="claude"),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.project_config", return_value=_PROJ_STUB),
    ):
        result = review_code.run(tmp_path, {"mode": "tdd"})

    assert result["status"] == "approved"
    assert result["provider"] == "claude"
    # Unlike the reviewer transport, this path CAN report these — so it must.
    assert result["cost_usd"] == 0.42
    assert result["duration_sec"] == 12.5
    assert result["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 9. #172 review IR-001 / IR-002 — post-invocation error paths and attribution
# ---------------------------------------------------------------------------


def _rc_patches(tmp_path, rwf_return, *, fp_return=None):
    from unittest.mock import MagicMock, patch

    stack = [
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=MagicMock()),
        patch("phase_runners.review_code.review_with_fallback", return_value=rwf_return),
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
        patch("phase_runners.review_code.git_rev_parse", return_value="deadbeef"),
        patch("phase_runners.review_code._is_ancestor", return_value=False),
        patch("phase_runners.review_code._incremental_diff_nonempty", return_value=False),
    ]
    if fp_return is not None:
        stack.append(patch("phase_runners.review_code.review_with_fallback_for_provider", return_value=fp_return))
    return stack


def _rc_state(**extra):
    s = {
        "mode": "agent-pair",
        "base_branch": "main",
        "models": {"implementer": "claude-sonnet-4-6", "reviewer": "codex"},
        "review_items": [],
    }
    s.update(extra)
    return s


def test_review_code_ceiling_abort_after_dispatch_still_records(tmp_path):
    """IR-001: the POST-dispatch wall-clock ceiling return happens AFTER a reviewer
    ran, so it must still record the round. Otherwise a ceiling-aborted review
    vanishes from telemetry and review_rounds under-counts.

    Distinct from the PRE-dispatch skip, which correctly records nothing because no
    reviewer was invoked. The monotonic sequence forces accrual past the ceiling
    during the dispatch, which is what selects the post-dispatch branch.
    """
    import contextlib
    from unittest.mock import patch

    import phase_runners.review_code as review_code

    (tmp_path / ".redteam").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        "[models]\nreviewer = 'codex'\n\n[models.review_ceilings]\nmax_wall_clock_sec = 10\n",
        encoding="utf-8",
    )
    # Start just below; t0=0.0 → t1=5.0 accrues 5.0, total 14.0 >= 10.
    state = _rc_state(review_code_wall_clock_sec=9.0)
    seq = iter([0.0, 5.0])
    with contextlib.ExitStack() as st:
        for p in _rc_patches(tmp_path, _ok_review("CHANGES_REQUESTED")):
            st.enter_context(p)
        mock_time = st.enter_context(patch("phase_runners.review_code.time"))
        mock_time.monotonic = lambda: next(seq)
        result = review_code.run(tmp_path, state)

    assert result.get("ceiling_hit") == "max_wall_clock_sec"
    assert result["provider"] == "codex", "a reviewer ran; the round must still be recorded"


def test_review_code_staged_first_pass_records_the_provider_that_ran(tmp_path):
    """IR-002: a non-APPROVED first-pass result is returned as-is, so telemetry must
    name the FIRST-PASS provider, not the configured frontier one."""
    import contextlib

    import phase_runners.review_code as review_code

    (tmp_path / ".redteam").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(
        "[models]\nreviewer = 'codex'\n\n[models.review_stages]\nfirst_pass_reviewer = 'claude'\nescalate_after = 5\n",
        encoding="utf-8",
    )
    fp = dict(_ok_review("CHANGES_REQUESTED"))
    fp["provider_used"] = "claude"  # set by the adapter layer for the actual runner
    state = _rc_state(implement_round_count=1)
    with contextlib.ExitStack() as st:
        for p in _rc_patches(tmp_path, _ok_review("APPROVED"), fp_return=fp):
            st.enter_context(p)
        result = review_code.run(tmp_path, state)

    assert result["status"] == "changes_requested"
    assert result["provider"] == "claude", "first-pass round must not be labelled with the frontier provider"


def test_fallback_result_reports_the_fallback_provider(tmp_path):
    """IR-002: when an automatic fallback produced the review, provider_used names
    the fallback — attributing it to the configured primary records a wrong label."""
    import contextlib

    import phase_runners.plan_review as plan_review

    (tmp_path / "outcome.md").write_text("x\n", encoding="utf-8")
    fb = dict(_ok_review("APPROVED"))
    fb["fallback_audit"] = "primary 'codex' failed. Fell back to 'claude'."
    fb["provider_used"] = "claude"
    from unittest.mock import MagicMock, patch

    with contextlib.ExitStack() as st:
        st.enter_context(patch("phase_runners.plan_review.get_reviewer_adapter", return_value=MagicMock()))
        st.enter_context(patch("phase_runners.plan_review.review_with_fallback", return_value=fb))
        st.enter_context(patch("phase_runners.plan_review.compute_repo_diff", return_value=""))
        st.enter_context(patch("phase_runners.plan_review.repo_root", return_value=tmp_path))
        result = plan_review.run(tmp_path, {"mode": "agent-pair", "models": {"reviewer": "codex"}})

    assert result["provider"] == "claude"
