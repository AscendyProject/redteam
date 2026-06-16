"""Headless reviewer adapter — resolver, Codex adapter, runner wiring, gating.

The agent-pair review gates (plan_review / review_code) can now be produced by
a headless reviewer adapter (`state.models.reviewer == "codex"`) instead of a
manual Codex paste + sentinel. These tests pin:
- the resolver maps "codex" -> adapter, everything else -> None (legacy manual);
- the Codex adapter parses REVIEW_DECISION from stdout and fails closed;
- the runners call the adapter, persist its output, and parse the decision;
- the orchestrator skips the manual sentinel wait when an adapter is configured,
  but still blocks when it is not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from adapters import get_reviewer_adapter, get_worker_adapter  # noqa: E402
from adapters.claude import ClaudeReviewerAdapter, ClaudeWorkerAdapter  # noqa: E402
from adapters.codex import CodexReviewerAdapter, CodexWorkerAdapter  # noqa: E402


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


# ---- resolver ----


def test_resolver_maps_codex_else_none() -> None:
    assert isinstance(get_reviewer_adapter({"models": {"reviewer": "codex"}}), CodexReviewerAdapter)
    # IR-005: absent models / absent reviewer inherit the config default (codex),
    # matching claude_model_for_role — legacy agent-pair tasks stay headless.
    assert isinstance(get_reviewer_adapter({}), CodexReviewerAdapter)
    assert isinstance(get_reviewer_adapter({"models": {}}), CodexReviewerAdapter)
    # claude is now a registered headless reviewer too (registry expansion).
    assert isinstance(get_reviewer_adapter({"models": {"reviewer": "claude"}}), ClaudeReviewerAdapter)
    # An explicit non-adapter value opts back out to the manual flow.
    assert get_reviewer_adapter({"models": {"reviewer": "human"}}) is None
    assert get_reviewer_adapter({"models": {"reviewer": "gemini"}}) is None


# ---- Codex adapter (subprocess mocked) ----


def _fake_proc(returncode: int, stdout: str, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def test_codex_adapter_parses_decision() -> None:
    fake = _fake_proc(0, "work log...\nIR-001 ...\nREVIEW_DECISION: APPROVED\n")
    with patch("adapters.codex.subprocess.run", return_value=fake):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["decision"] == "APPROVED"
    assert r["parse_status"] == "ok"
    assert "REVIEW_DECISION: APPROVED" in r["raw"]


def test_codex_adapter_decodes_as_utf8() -> None:
    """#32: subprocess.run must pin encoding="utf-8" so reviewer output with
    non-ASCII (em-dash, Korean) decodes consistently instead of crashing on a
    non-UTF-8 platform default (e.g. cp949 on Korean Windows)."""
    fake = _fake_proc(0, "REVIEW_DECISION: APPROVED\n")
    with patch("adapters.codex.subprocess.run", return_value=fake) as run:
        CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert run.call_args.kwargs["encoding"] == "utf-8"


def test_codex_adapter_missing_decision_unparseable() -> None:
    with patch("adapters.codex.subprocess.run", return_value=_fake_proc(0, "no decision here")):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["decision"] == "MISSING"
    assert r["parse_status"] == "unparseable"


def test_codex_adapter_not_found_is_error() -> None:
    with patch("adapters.codex.subprocess.run", side_effect=FileNotFoundError()):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"
    assert "Install" in r["raw"] and 'reviewer="human"' in r["raw"]  # actionable


def test_codex_adapter_timeout_fails_closed() -> None:
    """A hung / over-long reviewer must fail closed (not block the batch)."""
    with patch(
        "adapters.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1),
    ):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"
    assert "codex login" in r["raw"]  # timeout also gets the auth/recovery hint


def test_codex_error_carries_hint_and_omits_stderr() -> None:
    """A fail-closed error must (a) tell the operator how to recover (codex
    login / reviewer=human) and (b) NOT leak stderr, which can carry the auth
    token (IR-002)."""
    with patch(
        "adapters.codex.subprocess.run",
        return_value=_fake_proc(1, "review body", stderr="Authorization: Bearer sk-SECRETTOKEN123"),
    ):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["parse_status"] == "error"
    assert "codex login" in r["raw"] and 'reviewer="human"' in r["raw"]
    assert "sk-SECRETTOKEN123" not in r["raw"]  # stderr omitted


def test_codex_adapter_declares_capabilities() -> None:
    """IR-003: the adapter declares its capabilities incl. a hard timeout that
    bounds a hung/over-long reviewer."""
    caps = CodexReviewerAdapter.capabilities
    assert caps["native_diff_review"] is False  # codex exec; review --base is a later milestone
    assert caps["timeout_sec"] >= 1


def test_codex_adapter_nonzero_exit_fails_closed() -> None:
    """IR-002: a non-zero exit must fail closed even if stdout ends in APPROVED."""
    fake = _fake_proc(1, "partial...\nREVIEW_DECISION: APPROVED\n", stderr="fatal")
    with patch("adapters.codex.subprocess.run", return_value=fake):
        r = CodexReviewerAdapter().review(
            role="review_code", prompt="x", cwd=Path("."), target={"kind": "branch_diff", "base": "main"}
        )
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"


# ---- runner wiring ----


def test_plan_review_runner_uses_adapter(tmp_path) -> None:
    """The runner delegates the headless review to the fallback ladder
    (review_with_fallback) and maps a valid APPROVED result to status=approved."""
    from phase_runners import plan_review

    fake = MagicMock()  # non-None adapter → headless branch is entered
    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=fake),
        patch(
            "phase_runners.plan_review.review_with_fallback",
            return_value={
                "decision": "APPROVED",
                "raw": "review body\nREVIEW_DECISION: APPROVED",
                "parse_status": "ok",
            },
        ) as rwf,
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        res = plan_review.run(tmp_path, {"models": {"reviewer": "codex"}})
    assert res["status"] == "approved"
    assert (tmp_path / "plan_review.md").read_text(encoding="utf-8").endswith("APPROVED")
    rwf.assert_called_once()


def test_review_code_runner_uses_adapter_in_agent_pair(tmp_path) -> None:
    from phase_runners import review_code

    fake = MagicMock()
    with (
        patch("phase_runners.review_code.get_reviewer_adapter", return_value=fake),
        patch(
            "phase_runners.review_code.review_with_fallback",
            return_value={
                "decision": "CHANGES_REQUESTED",
                "raw": "IR-001 ...\nREVIEW_DECISION: CHANGES_REQUESTED",
                "parse_status": "ok",
            },
        ) as rwf,
        patch("phase_runners.review_code.compute_repo_diff", return_value=""),
        patch("phase_runners.review_code.repo_root", return_value=tmp_path),
    ):
        res = review_code.run(tmp_path, {"mode": "agent-pair", "models": {"reviewer": "codex"}})
    assert res["status"] == "changes_requested"
    assert (tmp_path / "code_review.md").exists()
    rwf.assert_called_once()


def test_runner_fails_closed_on_unparseable_with_stray_decision(tmp_path) -> None:
    """IR-001: parse_status != 'ok' must fail closed; a stray REVIEW_DECISION in
    the raw body must NOT rescue an unparseable result into an approval."""
    from phase_runners import plan_review

    fake = MagicMock()
    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=fake),
        patch(
            "phase_runners.plan_review.review_with_fallback",
            return_value={
                "decision": "MISSING",
                "raw": "garbage output\nREVIEW_DECISION: APPROVED",
                "parse_status": "unparseable",
            },
        ),
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        res = plan_review.run(tmp_path, {"models": {"reviewer": "codex"}})
    assert res["status"] == "error"  # not "approved" despite the stray line


def test_headless_prompts_forbid_writes() -> None:
    """IR-004: headless prompts must instruct stdout-only (read-only sandbox)."""
    from phase_runners import plan_review, review_code

    for prompt in (plan_review._plan_review_prompt(Path("/t")), review_code._code_review_prompt(Path("/t"))):
        assert "stdout only" in prompt
        assert "DO NOT write" in prompt


def test_runner_falls_back_to_manual_without_adapter(tmp_path) -> None:
    """No adapter configured → legacy manual flow: error if the file is absent."""
    from phase_runners import plan_review

    with (
        patch("phase_runners.plan_review.get_reviewer_adapter", return_value=None),
        patch("phase_runners.plan_review.compute_repo_diff", return_value=""),
        patch("phase_runners.plan_review.repo_root", return_value=tmp_path),
    ):
        res = plan_review.run(tmp_path, {"models": {"reviewer": "human"}})
    assert res["status"] == "error"  # no plan_review.md present


# ---- worker adapter (step 4) ----


def test_worker_resolver_returns_claude() -> None:
    a = get_worker_adapter({"models": {"implementer": "claude-sonnet-4-6"}})
    assert isinstance(a, ClaudeWorkerAdapter)


def test_claude_worker_adapter_wraps_run_claude() -> None:
    """The adapter forwards to run_claude with the per-role model and returns a
    WorkerRunResult (returncode/stdout/stderr)."""
    rc = {"returncode": 0, "stdout": "out", "stderr": "err", "parsed_json": None}
    with patch("adapters.claude.run_claude", return_value=rc) as run:
        r = ClaudeWorkerAdapter({"models": {"implementer": "claude-sonnet-4-6"}}).invoke(
            role="implementer", agent="impl-agent", prompt="p", cwd=Path(".")
        )
    assert r == {"returncode": 0, "stdout": "out", "stderr": "err"}
    run.assert_called_once()
    _, kwargs = run.call_args
    assert kwargs["agent"] == "impl-agent"
    assert kwargs["model"] == "claude-sonnet-4-6"  # per-role model resolved


# ---- codex worker adapter (A2 — role reversal) ----


def test_worker_resolver_returns_codex() -> None:
    """implementer=codex routes the worker to the codex provider (role reversal:
    codex writes the code). Selection keys off the implementer role — the
    canonical code-writing role — so a 'codex main' config (planner+implementer
    = codex) runs every worker phase on codex."""
    a = get_worker_adapter({"models": {"implementer": "codex"}})
    assert isinstance(a, CodexWorkerAdapter)


def test_worker_resolver_defaults_to_claude() -> None:
    """Absent models inherit the config default (implementer=claude-sonnet-4-6),
    so the legacy/default path stays on the claude worker — no behavior change."""
    assert isinstance(get_worker_adapter({}), ClaudeWorkerAdapter)
    assert isinstance(get_worker_adapter({"models": {}}), ClaudeWorkerAdapter)


def test_codex_worker_runs_workspace_write() -> None:
    """The codex worker writes the tree via `codex exec --sandbox
    workspace-write`, sends the prompt on stdin, and returns a WorkerRunResult."""
    fake = _fake_proc(0, "wrote files", stderr="")
    with patch("adapters.codex.subprocess.run", return_value=fake) as run:
        r = CodexWorkerAdapter().invoke(role="implementer", agent="implementer", prompt="do the thing", cwd=Path("."))
    assert r == {"returncode": 0, "stdout": "wrote files", "stderr": ""}
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["codex", "exec"]
    assert "--sandbox" in cmd and "workspace-write" in cmd
    # read-only must NOT be how a mutating worker runs.
    assert "read-only" not in cmd
    assert "do the thing" in run.call_args.kwargs["input"]


def test_codex_worker_inlines_agent_rules(tmp_path) -> None:
    """Codex has no `--agent`; the agent definition the prompt references
    ('Follow your agent definition exactly') must be inlined into the prompt."""
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "implementer.md").write_text(
        "---\nname: implementer\nmodel: claude-sonnet-4-6\n---\n\nStay within Affected files. UNIQUE_RULE_MARKER.\n",
        encoding="utf-8",
    )
    fake = _fake_proc(0, "ok")
    with patch("adapters.codex.subprocess.run", return_value=fake) as run:
        CodexWorkerAdapter().invoke(role="implementer", agent="implementer", prompt="TASK_BODY_MARKER", cwd=tmp_path)
    sent = run.call_args.kwargs["input"]
    assert "UNIQUE_RULE_MARKER" in sent  # agent body inlined
    assert "TASK_BODY_MARKER" in sent  # task prompt preserved
    assert "name: implementer" not in sent  # frontmatter stripped


def test_codex_worker_missing_agent_md_fails_closed(tmp_path) -> None:
    """IR-001: a missing/empty agent .md must fail the phase WITHOUT running the
    mutating worker — codex has no `--agent`, so the inlined rules are the only
    scope on a workspace-write run; running unscoped is fail-open."""
    with patch("adapters.codex.subprocess.run") as run:
        r = CodexWorkerAdapter().invoke(
            role="implementer", agent="nonexistent", prompt="TASK_BODY_MARKER", cwd=tmp_path
        )
    assert r["returncode"] != 0
    assert "nonexistent" in r["stderr"]  # names the missing agent path
    run.assert_not_called()  # codex never invoked


def test_codex_worker_omits_stderr(tmp_path) -> None:
    """IR-002: raw codex stderr (which can carry credentials) is never forwarded;
    the returncode still signals the failure, stdout still carries the work log."""
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "implementer.md").write_text("---\nname: implementer\n---\n\nRules.\n", encoding="utf-8")
    fake = _fake_proc(1, "work log tail", stderr="Authorization: Bearer sk-SECRETTOKEN123")
    with patch("adapters.codex.subprocess.run", return_value=fake):
        r = CodexWorkerAdapter().invoke(role="implementer", agent="implementer", prompt="x", cwd=tmp_path)
    assert r["returncode"] == 1  # failure still signaled
    assert r["stdout"] == "work log tail"  # stdout preserved
    assert "sk-SECRETTOKEN123" not in r["stderr"]  # raw stderr omitted


def test_codex_worker_not_found_returns_127() -> None:
    """codex missing on PATH → non-zero returncode so the runner reports the
    phase as errored (the runner checks returncode != 0)."""
    with patch("adapters.codex.subprocess.run", side_effect=FileNotFoundError()):
        r = CodexWorkerAdapter().invoke(role="implementer", agent="implementer", prompt="x", cwd=Path("."))
    assert r["returncode"] == 127
    assert "not found" in r["stderr"]


def test_codex_worker_timeout_returns_124() -> None:
    """A hung worker fails the phase (non-zero) rather than hanging the batch."""
    with patch(
        "adapters.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1),
    ):
        r = CodexWorkerAdapter().invoke(role="implementer", agent="implementer", prompt="x", cwd=Path("."))
    assert r["returncode"] == 124
    assert "timed out" in r["stderr"]


# ---- orchestrator gating ----


def _seed_task(tmp_path: Path, state: dict) -> Path:
    task_dir = tmp_path / "batch" / "tasks" / "task-001-demo"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")
    (task_dir / "outcome.md").write_text("plan", encoding="utf-8")
    (task_dir / "outcome.approved").write_text("", encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return task_dir


def test_orchestrator_skips_sentinel_with_headless_reviewer(monkeypatch, tmp_path) -> None:
    """agent-pair + reviewer=codex: plan_review runs without a plan_review.done
    sentinel (the adapter produces the review synchronously)."""
    orch = _load_orchestrator_module()
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "plan_review",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "review_items": [],
        "models": {"reviewer": "codex"},
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    task_dir = _seed_task(tmp_path, state)
    monkeypatch.setattr(
        orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"proj/{task_id}"
    )
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)
    ran = {"plan_review": False}

    def _fake_plan_review(td, st):
        ran["plan_review"] = True
        # Return ask_user so the run halts right after this phase instead of
        # advancing into implement (which would need a real git tree).
        return {"status": "ask_user", "feedback": "halt", "log": "", "diff": ""}

    monkeypatch.setitem(orch.PHASE_RUNNERS, "plan_review", _fake_plan_review)

    orch.process_task(task_dir)
    assert ran["plan_review"] is True  # ran without a plan_review.done sentinel


def test_orchestrator_blocks_without_adapter(monkeypatch, tmp_path) -> None:
    """No reviewer adapter: plan_review still blocks on the manual sentinel."""
    orch = _load_orchestrator_module()
    state = {
        "task_id": "task-001-demo",
        "mode": "agent-pair",
        "phase": "plan_review",
        "phases_completed": ["plan_outcome"],
        "next_phase": "plan_review",
        "review_items": [],
        "models": {"reviewer": "human"},
        "verification": {"last_exit_code": 0},
        "escape": {"ask_user": False, "reason": None, "return_phase": None},
    }
    task_dir = _seed_task(tmp_path, state)
    monkeypatch.setattr(
        orch, "_ensure_task_branch", lambda task_id, repo, branch_prefix, base_branch: f"proj/{task_id}"
    )
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)

    outcome = orch.process_task(task_dir)
    assert outcome == "blocked_on_human_gate"


def test_status_summary_surfaces_last_failure() -> None:
    """IR-001: `orchestrator status` must show the failure reason so the
    recovery hint isn't buried in state.json."""
    orch = _load_orchestrator_module()
    line = orch._summary_lines({"next_phase": "review_code", "last_failure_reason": "error"})
    assert "last_failure=error" in line


def test_status_omits_failure_log_but_gives_codex_hint(capsys, tmp_path) -> None:
    """IR-004: status must NOT echo last_failure_log (a phase's raw stderr there
    can carry tokens) but must still point to codex-login recovery."""
    orch = _load_orchestrator_module()
    state = {
        "task_id": "task-001-demo",
        "next_phase": "review_code",
        "last_failure_reason": "error",
        "last_failure_log": "stderr: Authorization: Bearer sk-SECRETTOKEN",
    }
    task_dir = _seed_task(tmp_path, state)
    orch.cmd_status(task_dir.parent.parent)
    out = capsys.readouterr().out
    assert "sk-SECRETTOKEN" not in out  # raw log not echoed (IR-004 / IR-002)
    assert "codex login" in out  # actionable recovery hint
    assert "last_failure=error" in out  # reason surfaced (IR-001)
