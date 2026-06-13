#!/usr/bin/env python3
"""redteam orchestrator.

Usage:
    python3 .redteam/workflows/orchestrator.py start  <batch-dir>
    python3 .redteam/workflows/orchestrator.py resume <batch-dir>
    python3 .redteam/workflows/orchestrator.py status <batch-dir>

Walks each task in <batch-dir>/tasks/ through the 8-phase pipeline,
persists state.json after every phase, blocks at human gates (sentinel files),
retries on CHANGES_REQUESTED up to `max_retries_per_phase`, and defers tasks
that stall (same log + same diff repeatedly).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

# Make `phase_runners.*` importable when this file is invoked as a script
# (`python3 .redteam/workflows/orchestrator.py ...`) rather than via `python -m`.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from phase_runners import (  # type: ignore[import-not-found]  # noqa: E402
    create_pr,
    implement,
    plan_outcome,
    plan_review,
    rescue,
    review_code,
    verify_test,
    write_test,
)
from phase_runners._base import (  # type: ignore[import-not-found]  # noqa: E402
    PhaseResult,
    compute_branch_changed_paths,
    extract_verification_commands,
    repo_root,
    validate_verification_commands,
)
from adapters import (  # type: ignore[import-not-found]  # noqa: E402
    get_reviewer_adapter,
    get_worker_adapter,
    reviewer_provider,
    worker_provider,
)
from config import load_config, resolve_tier  # type: ignore[import-not-found]  # noqa: E402


# ---------- phase order & runner registry ----------

TDD_PHASE_ORDER: list[str] = [
    "plan_outcome",
    "human_gate_outcome",
    "write_test",
    "verify_test",
    "implement",
    "review_code",
    "rescue",
    "human_gate_rescue",
    "create_pr",
    "done",
]

AGENT_PAIR_PHASE_ORDER: list[str] = [
    "plan_outcome",
    "plan_review",
    "human_gate_outcome",
    "implement",
    "review_code",
    # rescue + its human gate must be in the order so that a rescue
    # (entered conditionally via next_phase="rescue") advances to
    # human_gate_rescue rather than falling through to "done". The normal
    # path skips them: review_code-approved sets next_phase="create_pr"
    # explicitly, so _next_phase is never consulted for review_code.
    "rescue",
    "human_gate_rescue",
    "create_pr",
    "done",
]

# The two pipeline modes (issue #36). `mode` decides WHICH review gates run, so an
# unknown value must fail closed, never silently fall through to one of them.
VALID_MODES: tuple[str, ...] = ("agent-pair", "tdd")


PhaseRunner = Callable[[Path, dict[str, Any]], PhaseResult]


PHASE_RUNNERS: dict[str, PhaseRunner] = {
    "plan_outcome": plan_outcome.run,
    "plan_review": plan_review.run,
    "write_test": write_test.run,
    "verify_test": verify_test.run,
    "implement": implement.run,
    "review_code": review_code.run,
    "rescue": rescue.run,
    "create_pr": create_pr.run,
}


# Reviewer phase → worker phase to re-invoke when REVIEW_DECISION is CHANGES_REQUESTED.
# A rejected review means the worker's output was inadequate, so we go back to the worker
# (carrying the reviewer's feedback as input), not retry the reviewer with the same input.
REVIEWER_BACKTRACK: dict[str, str] = {
    "plan_review": "plan_outcome",
    "verify_test": "write_test",
    "review_code": "implement",
}


GATE_SENTINELS: dict[str, str] = {
    "human_gate_outcome": "outcome.approved",
    "ask_user": "ask_user.resolved",
    "human_gate_rescue": "rescue.reviewed",
    "human_gate_pr": "pr.reviewed",
}


MANUAL_PHASE_SENTINELS: dict[str, str] = {
    "plan_review": "plan_review.done",
    "review_code": "code_review.done",
    "rescue": "rescue.done",
}

# Read-only reviewer phases that a headless reviewer adapter can produce
# synchronously (skipping the manual sentinel). rescue is excluded — it is a
# mutating flow, not a read-only review.
REVIEWER_PHASES: frozenset[str] = frozenset({"plan_review", "review_code"})


# ---------- state I/O ----------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(task_dir: Path) -> dict[str, Any]:
    state_path = task_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(
            f"state.json not found in {task_dir}. Add an input.md to the task "
            f"directory and run `orchestrator.py start <batch>` — it seeds state.json "
            f"from .redteam/templates/state.template.json automatically."
        )
    text = state_path.read_text(encoding="utf-8")
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError(f"state.json in {task_dir} is not a JSON object")
    return obj


def save_state(task_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp = task_dir / "state.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(task_dir / "state.json")


# ---------- phase progression ----------


def _mode(state: dict[str, Any]) -> str:
    return str(state.get("mode") or "agent-pair")


def _build_tier_phase_order(profile: Any) -> list[str]:
    """Build a coherent phase order from a tier profile's declarative toggles
    (issue #13). The engine owns the structure, so every review/gate combination
    composes safely — the conditionally-entered `rescue` slot and the create_pr
    tail are always placed correctly, never skipped by an unsafe custom order.

    - `review` adds the adversarial pair (plan_review + review_code) and the
      rescue escalation slot; without it the task is single-agent.
    - `gates` inserts the chosen human gates; the lean default is none.
    """
    order = ["plan_outcome"]
    if profile.review:
        order.append("plan_review")
    if "outcome" in profile.gates:
        order.append("human_gate_outcome")
    order.append("implement")
    if profile.review:
        order.append("review_code")
        order.append("rescue")  # conditionally entered; must be present so its completion resolves
        if "rescue" in profile.gates:
            order.append("human_gate_rescue")
    order.append("create_pr")
    if "pr" in profile.gates:
        order.append("human_gate_pr")
    order.append("done")
    return order


def _phase_order(state: dict[str, Any]) -> list[str]:
    # A resolved tier profile (issue #13) overrides the order for this task.
    tier_phases = state.get("tier_phases")
    if tier_phases:
        return list(tier_phases)
    if _mode(state) == "agent-pair":
        return AGENT_PAIR_PHASE_ORDER
    return TDD_PHASE_ORDER  # mode == "tdd"; validated against VALID_MODES upstream


def _adversarial_pairing_error(state: dict[str, Any]) -> str | None:
    """Fail-closed guard on the harness's core promise: when a task actually runs
    a headless reviewer phase, the reviewer must resolve to a DIFFERENT provider
    than the worker. A reviewer silently pointed at the worker's own provider
    (e.g. an agent flipping reviewer to "claude" while the worker is also claude)
    collapses the adversarial pair into self-review — the code gets reviewed by
    the same model that wrote it, which defeats the entire point of the harness.
    Returns an error message if that collapse is configured, else None.

    Enforced exactly when a phase that runs the HEADLESS reviewer adapter is in
    the resolved order:
      - `plan_review` always uses the headless adapter (plan_review.run calls
        get_reviewer_adapter regardless of mode — e.g. a tier-routed order with
        review=true even while mode="tdd"), and
      - `review_code` uses the headless adapter ONLY in agent-pair mode; in tdd it
        reviews via the WORKER adapter (get_worker_adapter role="reviewer"), the
        same-agent-test-first-by-design case, so it is not a headless self-review
        risk and must not trip the guard (avoids the #28 false positive).
    A `review=false` single-agent tier (neither phase) or a human/manual reviewer
    (no headless adapter → reviewer_provider None) passes by design.
    """
    order = _phase_order(state)
    runs_headless_review = "plan_review" in order or ("review_code" in order and _mode(state) == "agent-pair")
    if not runs_headless_review:
        return None
    rp = reviewer_provider(state)  # None → manual/human reviewer, a distinct adversary
    if rp is None:
        return None
    wp = worker_provider(state)
    if rp != wp:
        return None
    return (
        f"adversarial pairing collapsed: the reviewer and the worker both resolve to the "
        f"'{wp}' provider, so the code would be reviewed by the same model that wrote it "
        f"(self-review). The redteam harness requires a cross-provider pair. Fix "
        f".redteam/config.toml [models]: point reviewer at a different provider than the "
        f'implementer (e.g. implementer=claude-*, reviewer="codex"), or set reviewer="human" '
        f"for a manual review, or use a review=false tier for an explicit single-agent path."
    )


def _parse_input_frontmatter(task_dir: Path) -> tuple[int | None, list[str] | None, str | None]:
    """Read an optional leading TOML front-matter block from input.md, fenced by
    `+++` lines, and return its `tier` (int), `paths` (list[str]), and `mode`
    (str) if present.

    No input.md, no fence, or a malformed block → (None, None, None): the task is
    then treated as unclassified and resolves to the safe default tier and the
    state's existing mode. TOML (stdlib tomllib) keeps the zero-dependency promise.
    `mode` is returned raw (any string) and enum-validated by the caller, so a
    typo fails closed rather than silently selecting a pipeline.
    """
    path = task_dir / "input.md"
    if not path.exists():
        return None, None, None
    text = path.read_text(encoding="utf-8")
    if not text.startswith("+++"):
        return None, None, None
    end = text.find("\n+++", 3)
    if end == -1:
        return None, None, None
    block = text[3:end].strip()
    try:
        fm = tomllib.loads(block)
    except tomllib.TOMLDecodeError:
        return None, None, None
    tier = fm.get("tier")
    paths = fm.get("paths")
    mode = fm.get("mode")
    tier = tier if isinstance(tier, int) and not isinstance(tier, bool) else None
    paths = [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else None
    mode = mode if isinstance(mode, str) else None
    return tier, paths, mode


def _changed_paths(cwd: Path) -> list[str]:
    """Ground-truth changed paths on the task branch, for the tier downgrade check
    (issue #19) — the REAL diff, not the paths a task DECLARES in its front-matter.
    Delegates to the NUL-delimited name-only helper so special-char paths can't be
    dropped (a missed path would fail open and let a downgrade slip through)."""
    return compute_branch_changed_paths(cwd=cwd)


def _next_phase(state: dict[str, Any], current: str) -> str:
    phase_order = _phase_order(state)
    if current not in phase_order:
        return "done"
    idx = phase_order.index(current)
    if idx + 1 >= len(phase_order):
        return "done"
    return phase_order[idx + 1]


def _is_gate(phase: str) -> bool:
    return phase in GATE_SENTINELS


def _gate_satisfied(task_dir: Path, phase: str) -> bool:
    sentinel = GATE_SENTINELS.get(phase)
    if not sentinel:
        return False
    return (task_dir / sentinel).exists()


def _manual_phase_ready(task_dir: Path, phase: str) -> bool:
    sentinel = MANUAL_PHASE_SENTINELS.get(phase)
    if not sentinel:
        return True
    return (task_dir / sentinel).exists()


def _set_next_action_for_manual_phase(task_dir: Path, state: dict[str, Any], phase: str) -> None:
    prompt_map = {
        "plan_review": ".redteam/prompts/codex/plan_review.md",
        "review_code": ".redteam/prompts/codex/code_review.md",
        "rescue": ".redteam/prompts/codex/rescue.md",
    }
    output_map = {
        "plan_review": "plan_review.md",
        "review_code": "code_review.md",
        "rescue": "rescue_report.md",
    }
    sentinel = MANUAL_PHASE_SENTINELS[phase]
    state["next_action"] = {
        "who": "codex",
        "what": f"Use {prompt_map[phase]} to produce {task_dir / output_map[phase]}, then touch {task_dir / sentinel}.",
        "reads": ["input.md", "outcome.md", "state.json"],
        "writes": [output_map[phase], sentinel],
    }


def _parse_user_decision(path: Path) -> str | None:
    if not path.exists():
        return None
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if not last.startswith("USER_DECISION:"):
        return None
    value = last.split(":", 1)[1].strip()
    if value in {"APPROVE", "REVISE_PLAN", "REVISE_IMPLEMENTATION", "ABANDON"}:
        return value
    return None


def _read_user_response_body(path: Path) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    body = [line for line in lines if not line.strip().startswith("USER_DECISION:")]
    return "\n".join(body).strip()


def _append_user_response_to_feedback(state: dict[str, Any], response: str) -> None:
    if not response:
        return
    state["last_user_response"] = response
    existing = state.get("last_failure_log") or ""
    state["last_failure_log"] = existing + "\n\n## User response\n\n" + response


def _close_phase_review_items(state: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    items = state.get("review_items")
    if not isinstance(items, list):
        return []
    updated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("phase") == phase and item.get("status") == "open":
            item = dict(item)
            item["status"] = "closed_at_phase_exit"
            item["phase_resolved_at"] = phase
        updated.append(item)
    return updated


def _clear_manual_sentinel(task_dir: Path, phase: str) -> None:
    sentinel = MANUAL_PHASE_SENTINELS.get(phase)
    if not sentinel:
        return
    path = task_dir / sentinel
    if path.exists():
        path.unlink()


def _clear_manual_phase_artifacts(task_dir: Path, phase: str) -> None:
    _clear_manual_sentinel(task_dir, phase)
    review_files = {
        "plan_review": "plan_review.md",
        "review_code": "code_review.md",
        "rescue": "rescue_report.md",
    }
    filename = review_files.get(phase)
    if filename:
        path = task_dir / filename
        if path.exists():
            previous = task_dir / f"{filename}.previous"
            path.replace(previous)


def _clear_ask_user_sentinel(task_dir: Path) -> None:
    sentinel = task_dir / GATE_SENTINELS["ask_user"]
    if sentinel.exists():
        sentinel.unlink()


def _archive_ask_user_response(task_dir: Path) -> None:
    path = task_dir / "ask_user_response.md"
    if path.exists():
        path.replace(task_dir / "ask_user_response.md.previous")


def _snapshot_verification_commands(task_dir: Path, state: dict[str, Any]) -> bool:
    outcome_path = task_dir / "outcome.md"
    try:
        outcome_text = outcome_path.read_text(encoding="utf-8")
        commands = extract_verification_commands(outcome_text)
        # Pin the plan-time verify command AND allowlist so post-implementer
        # re-validation cannot drift if the implementer edits config.toml
        # mid-round (IR-001). Both come from one config load.
        proj = load_config(repo_root()).project
        verify_command = proj.verify_command
        verify_allowlist = list(proj.verification_allowlist)
        validate_verification_commands(commands, verify_command, verify_allowlist)
    except (OSError, ValueError) as exc:
        state["last_failure_reason"] = "invalid_verification_commands"
        state["last_failure_log"] = str(exc)
        return False
    verification = state.setdefault("verification", {})
    verification["commands"] = commands
    verification["verify_command"] = verify_command
    verification["verify_allowlist"] = verify_allowlist
    verification["last_exit_code"] = None
    verification["last_output_path"] = "verification.log"
    verification["last_run_at"] = None
    return True


REVIEW_ITEM_RE = re.compile(
    r"^(?P<id>(?:PR|IR)-\d{3})\s+severity:(?P<severity>\w+)\s+status:(?P<status>\w+)\b",
    re.IGNORECASE,
)


def _sync_review_items(state: dict[str, Any], phase: str, review_text: str) -> None:
    existing = state.setdefault("review_items", [])
    if not isinstance(existing, list):
        existing = []
        state["review_items"] = existing

    by_id = {item.get("id"): item for item in existing if isinstance(item, dict) and item.get("id")}
    for line in review_text.splitlines():
        match = REVIEW_ITEM_RE.search(line)
        if not match:
            continue
        item_id = match.group("id").upper()
        severity = match.group("severity").lower()
        status = match.group("status").lower()
        prev = by_id.get(item_id)
        if prev is None:
            existing.append(
                {
                    "id": item_id,
                    "phase": phase,
                    "severity": severity,
                    "status": status,
                    "summary": line.strip(),
                    "carry_over_count": 1 if status == "open" else 0,
                }
            )
            continue
        prev["phase"] = phase
        prev["severity"] = severity
        prev["summary"] = line.strip()
        if status == "open" and prev.get("status") == "open":
            prev["carry_over_count"] = int(prev.get("carry_over_count") or 1) + 1
        elif status == "open":
            prev["carry_over_count"] = 1
        else:
            prev["carry_over_count"] = 0
        prev["status"] = status


def _has_open_blocker_at_or_above(state: dict[str, Any], phase: str, count: int) -> bool:
    items = state.get("review_items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if (
            item.get("phase") == phase
            and item.get("status") == "open"
            and item.get("severity") == "blocker"
            and int(item.get("carry_over_count") or 0) >= count
        ):
            return True
    return False


def _record_failure(state: dict[str, Any], result: PhaseResult) -> None:
    state["last_failure_reason"] = result["status"]
    state["last_failure_log"] = result["log"]
    state["last_failure_diff"] = result["diff"]


def _clear_failure(state: dict[str, Any]) -> None:
    state["last_failure_reason"] = None
    state["last_failure_log"] = None
    state["last_failure_diff"] = None
    state["last_user_response"] = None


def _is_stalled(state: dict[str, Any], result: PhaseResult, retries: int) -> bool:
    """Stall = previous attempt produced exactly the same log AND diff, twice or more.

    A single rejection isn't a stall (the next attempt may move things forward).
    Two consecutive rejections with identical artifacts is the signal that the
    sub-agent can't make progress on its own.
    """
    if retries < 2:
        return False
    prev_log = state.get("last_failure_log") or ""
    prev_diff = state.get("last_failure_diff") or ""
    return result["log"] == prev_log and result["diff"] == prev_diff


# ---------- per-task driver ----------

TaskOutcome = Literal[
    "done",
    "blocked_on_human_gate",
    "deferred",
    "error",
]


def _ensure_task_branch(task_id: str, repo: Path, branch_prefix: str = "redteam", base_branch: str = "main") -> str:
    """Ensure we're on the per-task branch `<branch_prefix>/<task_id>` before phases run.

    Steps:
      1. Stash local tracked/untracked changes so branch checkout is not blocked
      2. Checkout `base_branch` (clean slate per task)
      3. Pull --ff-only (best effort; OK if no remote)
      4. Create or switch to <branch_prefix>/<task_id>
      5. Pop the stash back onto the selected task branch

    Returns the branch name. Raises CalledProcessError if checkout fails, or
    RuntimeError if restoring the stash conflicts.
    """
    branch = f"{branch_prefix}/{task_id}"
    stash_msg = f"orchestrator-multi-task-{task_id}"

    stash_proc = subprocess.run(
        ["git", "stash", "push", "-u", "-m", stash_msg],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if stash_proc.returncode != 0:
        raise subprocess.CalledProcessError(
            stash_proc.returncode,
            ["git", "stash", "push", "-u", "-m", stash_msg],
            output=stash_proc.stdout,
            stderr=stash_proc.stderr,
        )
    stash_output = (stash_proc.stdout or "") + (stash_proc.stderr or "")
    stashed = "No local changes to save" not in stash_output

    try:
        subprocess.run(
            ["git", "checkout", base_branch],
            cwd=str(repo),
            check=True,
        )

        subprocess.run(
            ["git", "pull", "--ff-only", "origin", base_branch],
            cwd=str(repo),
            check=False,
            capture_output=True,
        )

        rev_parse = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if rev_parse.returncode != 0:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=str(repo),
                check=True,
            )
        else:
            subprocess.run(
                ["git", "checkout", branch],
                cwd=str(repo),
                check=True,
            )
    finally:
        if stashed:
            pop_proc = subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(repo),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if pop_proc.returncode != 0:
                raise RuntimeError(
                    f"stash pop conflict during _ensure_task_branch for {task_id}. "
                    "The stash remains in the stash list for manual recovery. "
                    f"stderr: {(pop_proc.stderr or '')[:1000]}"
                )

    return branch


def process_task(task_dir: Path) -> TaskOutcome:
    """Drive a single task through PHASE_ORDER until it blocks, errors, or finishes."""
    state = load_state(task_dir)
    state["mode"] = _mode(state)

    # Ensure correct branch before any phase runs.
    # Skip if task is already done or deferred (no work to do).
    next_phase_check = state.get("next_phase")
    if next_phase_check not in ("done", "deferred"):
        cfg = load_config(repo_root())
        try:
            branch = _ensure_task_branch(task_dir.name, repo_root(), cfg.project.branch_prefix, cfg.project.base_branch)
            state["branch"] = branch
            save_state(task_dir, state)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            state["last_failure_reason"] = "branch_setup_failed"
            if isinstance(e, subprocess.CalledProcessError):
                state["last_failure_log"] = (
                    f"_ensure_task_branch failed for {task_dir.name}: "
                    f"cmd={e.cmd!r} returncode={e.returncode} "
                    f"stderr={(e.stderr if e.stderr else '')!r}"
                )
            else:
                state["last_failure_log"] = f"_ensure_task_branch failed for {task_dir.name}: {e!r}"
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "error"

        fm_tier, fm_paths, fm_mode = _parse_input_frontmatter(task_dir)
        is_fresh_task = not state.get("phases_completed")

        # Pipeline mode (issue #36): a fresh task may select agent-pair vs tdd via
        # input.md front-matter (`+++ mode = "tdd" +++`); otherwise the existing /
        # template-seeded state mode stands. Validate against an explicit enum and
        # FAIL CLOSED on an unknown value — the old code silently fell through any
        # non-"agent-pair" value to the TDD order, so a typo ran the wrong review
        # gates unnoticed. Validation runs on every start/resume so a hand-edited
        # bad mode in state.json also fails closed.
        if fm_mode is not None and is_fresh_task:
            # When tier routing is configured the tier profile governs the phase
            # order (_phase_order gives tier_phases precedence), so a front-matter
            # mode would be silently ignored — reject it loudly. Keyed on cfg.tiers
            # (every task is tier-routed when [tiers] is configured), which also
            # catches a tier already persisted from a prior run, before this fresh
            # task ever completed a phase (PR-002).
            if cfg.tiers:
                state["last_failure_reason"] = "mode_tier_conflict"
                state["last_failure_log"] = (
                    "input.md front-matter sets `mode`, but this repo is tier-routed and the "
                    "tier profile governs the phase order — `mode` would be ignored. Remove `mode` "
                    "from the front-matter, or use an untiered repo to choose the pipeline mode."
                )
                state["next_phase"] = "deferred"
                save_state(task_dir, state)
                return "error"
            state["mode"] = fm_mode
        if state.get("mode") not in VALID_MODES:
            state["last_failure_reason"] = "invalid_mode"
            state["last_failure_log"] = (
                f"Unknown pipeline mode {state.get('mode')!r}. Valid modes: {list(VALID_MODES)}. "
                'Select it via input.md front-matter (`+++\\nmode = "agent-pair"\\n+++`) or state.json.'
            )
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "error"

        # Resolve the risk tier ONCE, at a fresh task's start (issue #13), only
        # when tier routing is configured. Stored in state so resume is stable.
        # cfg.tiers empty → skipped → behavior unchanged. Restricted to fresh
        # tasks (no phases completed yet) so a profile's phase order drives from
        # the start and an in-flight task is never re-routed mid-pipeline.
        if cfg.tiers and "tier" not in state and not state.get("phases_completed"):
            # A front-matter mode here was already rejected above (mode_tier_conflict),
            # so by this point either no mode was declared or we never reach here.
            try:
                tier = resolve_tier(cfg, fm_tier, fm_paths)
            except ValueError as e:
                state["last_failure_reason"] = "tier_resolution_failed"
                state["last_failure_log"] = str(e)
                state["next_phase"] = "deferred"
                save_state(task_dir, state)
                return "error"
            profile = cfg.tiers[tier]
            tier_order = _build_tier_phase_order(profile)
            state["tier_phases"] = tier_order
            state["next_phase"] = tier_order[0]  # drive the built order from its first phase
            if profile.models:
                state["models"] = {**(state.get("models") or {}), **profile.models}
            state["tier"] = tier
            save_state(task_dir, state)

        # Adversarial-pairing guard: refuse to run if the configured reviewer
        # collapses to the worker's own provider (self-review). Runs after tier
        # resolution so the final phase order + per-tier model overrides are in
        # effect, and on every start/resume (not just fresh tasks) so a config
        # edited mid-flight can't slip a same-model review past the gate.
        pairing_error = _adversarial_pairing_error(state)
        if pairing_error:
            state["last_failure_reason"] = "adversarial_pairing_violation"
            state["last_failure_log"] = pairing_error
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "error"

    while True:
        phase = state.get("next_phase") or _phase_order(state)[0]

        if phase == "done":
            state["phase"] = "done"
            save_state(task_dir, state)
            return "done"

        if phase == "deferred":
            save_state(task_dir, state)
            return "deferred"

        # --- human gate: block until sentinel exists ---
        if _is_gate(phase):
            if not _gate_satisfied(task_dir, phase):
                state["phase"] = phase
                save_state(task_dir, state)
                return "blocked_on_human_gate"
            # gate cleared → advance
            completed = state.setdefault("phases_completed", [])
            if phase not in completed:
                completed.append(phase)
            if phase == "ask_user":
                response_body = _read_user_response_body(task_dir / "ask_user_response.md")
                user_decision = _parse_user_decision(task_dir / "ask_user_response.md")
                _append_user_response_to_feedback(state, response_body)
                return_phase = state.get("escape", {}).get("return_phase")
                if user_decision == "APPROVE":
                    state["next_phase"] = "implement"
                elif user_decision == "REVISE_PLAN":
                    if isinstance(return_phase, str):
                        _clear_manual_phase_artifacts(task_dir, return_phase)
                    state["next_phase"] = "plan_outcome"
                elif user_decision == "REVISE_IMPLEMENTATION":
                    if isinstance(return_phase, str):
                        _clear_manual_phase_artifacts(task_dir, return_phase)
                    state["next_phase"] = "implement"
                elif user_decision == "ABANDON":
                    state["next_phase"] = "deferred"
                else:
                    state["last_failure_reason"] = "missing_user_decision"
                    state["last_failure_log"] = (
                        "ask_user_response.md must end with one of: "
                        "USER_DECISION: APPROVE, REVISE_PLAN, REVISE_IMPLEMENTATION, ABANDON"
                    )
                    save_state(task_dir, state)
                    return "blocked_on_human_gate"
                state.setdefault("escape", {})["ask_user"] = False
                _clear_ask_user_sentinel(task_dir)
                _archive_ask_user_response(task_dir)
                # If this escalation routes to implement but the verification
                # snapshot was never taken — the plan_review that normally
                # snapshots it escalated here instead of approving — take it now,
                # fail-closed. Otherwise implement runs with no snapshotted
                # commands and is falsely deferred (issue #35). When a snapshot
                # already exists (e.g. REVISE_IMPLEMENTATION after an approved
                # plan_review), the guard leaves it untouched.
                if (
                    state["next_phase"] == "implement"
                    and _mode(state) == "agent-pair"
                    and state.get("verification", {}).get("verify_command") is None
                ):
                    if not _snapshot_verification_commands(task_dir, state):
                        state["next_phase"] = "deferred"
                        save_state(task_dir, state)
                        return "error"
            else:
                if phase == "human_gate_rescue":
                    state["next_phase"] = "create_pr"
                else:
                    state["next_phase"] = _next_phase(state, phase)
            state["phase"] = phase
            save_state(task_dir, state)
            continue

        if _mode(state) == "agent-pair" and phase in MANUAL_PHASE_SENTINELS:
            # A configured headless reviewer adapter produces the review
            # synchronously inside the runner, so the manual sentinel wait is
            # skipped for the reviewer phases. rescue is a mutating flow (not a
            # read-only reviewer adapter) and stays manual.
            headless_reviewer = phase in REVIEWER_PHASES and get_reviewer_adapter(state) is not None
            if not headless_reviewer and not _manual_phase_ready(task_dir, phase):
                state["phase"] = phase
                _set_next_action_for_manual_phase(task_dir, state, phase)
                save_state(task_dir, state)
                return "blocked_on_human_gate"

        # --- regular phase: run its sub-agent ---
        runner = PHASE_RUNNERS.get(phase)
        if runner is None:
            state["last_failure_reason"] = "unknown_phase"
            state["last_failure_log"] = f"unknown phase: {phase}"
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "error"

        state["phase"] = phase
        save_state(task_dir, state)

        # Fail-closed guard: in agent-pair mode, implement requires outcome.md
        # (created by plan_outcome). When plan_outcome has completed but
        # outcome.md is absent (e.g. plan_outcome was mocked in a unit test),
        # launching the implement agent would block or fail with a confusing
        # missing-file error. Return an immediate error instead.
        if (
            _mode(state) == "agent-pair"
            and phase == "implement"
            and "plan_outcome" in state.get("phases_completed", [])
            and not (task_dir / "outcome.md").exists()
        ):
            state["last_failure_reason"] = "missing_outcome_md"
            state["last_failure_log"] = (
                f"outcome.md not found in {task_dir}; plan_outcome must create it before implement can run."
            )
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "error"

        # Tier downgrade guard (issue #19): the tier was resolved from the paths
        # the task DECLARED. Now that the real diff exists, re-resolve against the
        # actually-changed files just before shipping. If the real diff floors the
        # task at a HIGHER tier than it ran under (e.g. declared tier 2 but the
        # diff touches auth → tier 4), fail closed — refuse to create the PR under
        # a too-light posture. The operator re-runs at the correct tier (the
        # lighter review/gates already taken can't be retrofitted mid-run; that
        # auto-escalation is a separate, deferred feature).
        if phase == "create_pr" and "tier" in state:
            cfg = load_config(repo_root())
            try:
                effective = resolve_tier(cfg, state["tier"], _changed_paths(repo_root()))
            except ValueError as e:
                state["last_failure_reason"] = "tier_resolution_failed"
                state["last_failure_log"] = str(e)
                state["next_phase"] = "deferred"
                save_state(task_dir, state)
                return "error"
            if effective is not None and effective > state["tier"]:
                state["last_failure_reason"] = "tier_downgrade_detected"
                state["last_failure_log"] = (
                    f"task ran at tier {state['tier']} but its actual diff resolves to tier {effective} "
                    f"(a path trigger floors it higher than declared). Re-run this task declaring "
                    f"tier {effective} (or higher) so it gets the heavier review/gates before a PR."
                )
                state["next_phase"] = "deferred"
                save_state(task_dir, state)
                return "deferred"

        result = runner(task_dir, state)
        if _mode(state) == "agent-pair" and phase in {"plan_review", "review_code"}:
            _sync_review_items(state, phase, result["log"])

        retries_map: dict[str, int] = state.setdefault("retries", {})
        max_retries = int(state.get("max_retries_per_phase", 3))

        if result["status"] == "approved":
            completed = state.setdefault("phases_completed", [])
            if phase not in completed:
                completed.append(phase)
            if phase == "plan_review" and _mode(state) == "agent-pair":
                state["review_items"] = _close_phase_review_items(state, phase)
                if not _snapshot_verification_commands(task_dir, state):
                    if phase in completed:
                        completed.remove(phase)
                    _clear_manual_phase_artifacts(task_dir, phase)
                    state["next_phase"] = "plan_outcome"
                    save_state(task_dir, state)
                    continue
            # A review=false tier (issue #13) has no plan_review, so the
            # verification-allowlist snapshot (the IR-001 security boundary that
            # pins verify_command + allowlist before implement) is taken here, at
            # plan_outcome approval, instead. Same fail-closed contract: a bad
            # snapshot defers rather than letting implement run unpinned.
            if phase == "plan_outcome" and _mode(state) == "agent-pair" and "plan_review" not in _phase_order(state):
                if not _snapshot_verification_commands(task_dir, state):
                    state["next_phase"] = "deferred"
                    save_state(task_dir, state)
                    return "error"
            if phase == "review_code":
                # An approved review goes straight to create_pr in BOTH modes,
                # explicitly skipping the conditionally-entered `rescue` phase
                # that sits between review_code and create_pr in the order.
                # Without this, TDD mode would fall through `_next_phase` into
                # rescue and stall (#7.5 finding F-E). rescue is only ever
                # entered via the explicit `next_phase = "rescue"` branches.
                if _mode(state) == "agent-pair":
                    state["review_items"] = _close_phase_review_items(state, phase)
                state["next_phase"] = "create_pr"
            else:
                state["next_phase"] = _next_phase(state, phase)
            _clear_failure(state)
            save_state(task_dir, state)
            continue

        if result["status"] == "ask_user":
            _record_failure(state, result)
            escape = state.setdefault("escape", {})
            escape["ask_user"] = True
            escape["reason"] = result["feedback"][:1000]
            escape["return_phase"] = phase
            state["next_phase"] = "ask_user"
            save_state(task_dir, state)
            continue

        if result["status"] == "rescue_required":
            _record_failure(state, result)
            deferred = state.setdefault("deferred_requirements", [])
            deferred.append(
                {
                    "phase": phase,
                    "reason": "rescue_required",
                    "feedback": result["feedback"][:4000],
                }
            )
            state["next_phase"] = "rescue"
            save_state(task_dir, state)
            continue

        if (
            _mode(state) == "agent-pair"
            and phase == "plan_review"
            and result["status"] == "changes_requested"
            and _has_open_blocker_at_or_above(state, phase, 2)
        ):
            _record_failure(state, result)
            escape = state.setdefault("escape", {})
            escape["ask_user"] = True
            escape["reason"] = "plan review blocker carried over twice"
            escape["return_phase"] = phase
            state["next_phase"] = "ask_user"
            save_state(task_dir, state)
            continue

        if (
            _mode(state) == "agent-pair"
            and phase == "review_code"
            and result["status"] == "changes_requested"
            and _has_open_blocker_at_or_above(state, phase, 3)
        ):
            _record_failure(state, result)
            state["next_phase"] = "rescue"
            save_state(task_dir, state)
            continue

        if (
            _mode(state) == "agent-pair"
            and phase == "review_code"
            and result["status"] == "changes_requested"
            and int(retries_map.get("implement", 0)) >= 2
        ):
            _record_failure(state, result)
            state["next_phase"] = "rescue"
            save_state(task_dir, state)
            continue

        # CHANGES_REQUESTED or error path.
        # Agent-pair rescue conditions are evaluated before generic retry/defer:
        # 1. Reviewer emits RESCUE_REQUIRED.
        # 2. review_code blocker carries over 3 or more times.
        # 3. review_code still requests changes after 2 implement retries.
        # 4. Otherwise retry the worker phase, then defer at the retry ceiling.
        # If this is a reviewer phase that was rejected, we backtrack to its worker
        # phase and pass the reviewer's feedback to the worker. The retry budget
        # accumulates against the WORKER (where the actual fix happens), not the
        # reviewer, so the worker has its full max_retries to converge.
        worker_phase = REVIEWER_BACKTRACK.get(phase) if result["status"] == "changes_requested" else None
        budget_phase = worker_phase or phase

        retries_map[budget_phase] = retries_map.get(budget_phase, 0) + 1
        attempts = retries_map[budget_phase]
        stalled = _is_stalled(state, result, attempts)
        exceeded = attempts > max_retries

        if stalled or exceeded:
            deferred: list[dict[str, Any]] = state.setdefault("deferred_requirements", [])
            deferred.append(
                {
                    "phase": phase,
                    "backtrack_to": worker_phase,
                    "reason": "stalled" if stalled else "max_retries_exceeded",
                    "attempts": attempts,
                    "feedback": result["feedback"][:4000],
                }
            )
            _record_failure(state, result)
            state["next_phase"] = "deferred"
            save_state(task_dir, state)
            return "deferred"

        _record_failure(state, result)
        if worker_phase:
            # Backtrack: clear the reviewer phase's "completed" status so it re-runs
            # after the worker reproduces work.
            completed = state.setdefault("phases_completed", [])
            if phase in completed:
                completed.remove(phase)
            _clear_manual_phase_artifacts(task_dir, phase)
            state["next_phase"] = worker_phase
        # else: stay on current phase (worker retry path or error retry)
        save_state(task_dir, state)
        # loop continues — either the worker phase or the same phase re-attempts


# ---------- batch driver ----------


def list_tasks(batch_dir: Path) -> list[Path]:
    tasks_root = batch_dir / "tasks"
    if not tasks_root.is_dir():
        return []
    return sorted(p for p in tasks_root.iterdir() if p.is_dir())


def _seed_state(task_dir: Path) -> None:
    """Initialize state.json for a task that has an input.md but no state yet,
    from .redteam/templates/state.template.json. Fills task_id + created_at so the
    README's "drop in an input.md and run start" flow needs no manual seeding.
    """
    template_path = repo_root() / ".redteam" / "templates" / "state.template.json"
    state = json.loads(template_path.read_text(encoding="utf-8"))
    state["task_id"] = task_dir.name
    state["created_at"] = utc_now()
    save_state(task_dir, state)


def process_batch(batch_dir: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for task_dir in list_tasks(batch_dir):
        # A task dir with neither state.json nor input.md is not initializable.
        if not (task_dir / "state.json").is_file() and not (task_dir / "input.md").is_file():
            results[task_dir.name] = "no_input_md"
            continue
        try:
            # Auto-seed from the template on first run when a brief exists. Kept
            # inside the try so a corrupt template fails this task only, not the
            # whole batch.
            if not (task_dir / "state.json").is_file():
                _seed_state(task_dir)
            results[task_dir.name] = process_task(task_dir)
        except Exception as e:  # surfaced to user via status report
            results[task_dir.name] = f"error: {e!r}"
    return results


# ---------- CLI ----------


def _print_results(results: dict[str, str]) -> None:
    if not results:
        print("(no tasks found)")
        return
    for name, status in results.items():
        print(f"  {name}: {status}")


def _summary_lines(state: dict[str, Any]) -> str:
    completed = len(state.get("phases_completed", []))
    next_phase = state.get("next_phase", "?")
    deferred = len(state.get("deferred_requirements", []))
    suffix = ""
    if deferred:
        suffix = f" [DEFERRED x{deferred}]"
    elif next_phase == "done":
        suffix = " [done]"
    elif next_phase in GATE_SENTINELS:
        suffix = f" [GATE: touch {GATE_SENTINELS[next_phase]}]"
    elif next_phase in MANUAL_PHASE_SENTINELS:
        suffix = f" [CODEX: write review, then touch {MANUAL_PHASE_SENTINELS[next_phase]}]"
    reason = state.get("last_failure_reason")
    if reason:
        suffix += f" [last_failure={reason}]"
    return f"completed={completed} next={next_phase}{suffix}"


def cmd_start(batch_dir: Path) -> int:
    return _run_pipeline(batch_dir, label="start")


def cmd_resume(batch_dir: Path) -> int:
    return _run_pipeline(batch_dir, label="resume")


def _run_pipeline(batch_dir: Path, *, label: str) -> int:
    if not batch_dir.is_dir():
        print(f"error: batch directory not found: {batch_dir}", file=sys.stderr)
        return 2
    print(f"orchestrator {label}: {batch_dir}")
    results = process_batch(batch_dir)
    _print_results(results)
    blocked = [n for n, s in results.items() if s == "blocked_on_human_gate"]
    deferred = [n for n, s in results.items() if s == "deferred"]
    if blocked:
        print()
        print(
            f"⏸  {len(blocked)} task(s) blocked at human gates. Touch the sentinel and re-run with `resume`.",
            file=sys.stderr,
        )
    if deferred:
        print()
        print(
            f"⚠  {len(deferred)} task(s) moved to deferred_requirements. Inspect their state.json and decide manually.",
            file=sys.stderr,
        )
    return 0 if not blocked else 1


# ---------- gate-2 polling (wait-and-resume) ----------

POLL_INTERVAL_SEC = 30
MAX_POLL_DURATION_SEC = 4 * 60 * 60  # 4 hours total before bailing out


def _pr_state_via_gh(pr_url: str) -> dict[str, Any] | None:
    """Query GitHub for the PR's state via the `gh` CLI. Returns the parsed
    JSON object or None if the call failed.
    """
    proc = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "state,isDraft,reviewDecision"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _gate_satisfied_by_pr_state(state_data: dict[str, Any] | None) -> tuple[bool, str]:
    """Decide whether `human_gate_pr` should be considered satisfied based
    on the PR's GitHub state. Returns (satisfied, reason).

    Conservative policy: only `MERGED` or `CLOSED` (without merge) flip the
    gate. `OPEN` PRs — even when ready+approved — keep waiting because the
    user might still tweak before merging. The user can also `touch
    pr.reviewed` manually if they want to override this.
    """
    if state_data is None:
        return False, "could not query PR via gh"
    state = str(state_data.get("state") or "").upper()
    if state == "MERGED":
        return True, "merged"
    if state == "CLOSED":
        return True, "closed (without merge)"
    review = state_data.get("reviewDecision")
    is_draft = state_data.get("isDraft")
    return False, f"open (draft={is_draft}, review={review})"


def cmd_wait_and_resume(batch_dir: Path) -> int:
    """Poll GitHub for PR resolution on every gate-#2-blocked task and auto-
    advance once a PR is merged or closed. Equivalent to a human-driven
    `touch pr.reviewed` loop, just driven by `gh pr view` instead.
    """
    if not batch_dir.is_dir():
        print(f"error: batch directory not found: {batch_dir}", file=sys.stderr)
        return 2

    pending: list[tuple[Path, str]] = []
    for task_dir in list_tasks(batch_dir):
        state_path = task_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(state, dict):
            continue
        if state.get("next_phase") != "human_gate_pr":
            continue
        pr_url = state.get("pr_url")
        if not pr_url:
            url_path = task_dir / "pr_url.txt"
            if url_path.is_file():
                pr_url = url_path.read_text(encoding="utf-8").strip()
        if not isinstance(pr_url, str) or not pr_url.startswith("https://"):
            print(
                f"  {task_dir.name}: blocked at human_gate_pr but no valid pr_url",
                file=sys.stderr,
            )
            continue
        pending.append((task_dir, pr_url))

    if not pending:
        print("no tasks blocked at human_gate_pr — running normal resume")
        return _run_pipeline(batch_dir, label="resume")

    print(
        f"polling {len(pending)} task(s) for PR resolution every {POLL_INTERVAL_SEC}s "
        f"(timeout {MAX_POLL_DURATION_SEC // 60}min). Ctrl-C to stop."
    )
    deadline = time.monotonic() + MAX_POLL_DURATION_SEC

    while pending and time.monotonic() < deadline:
        next_pending: list[tuple[Path, str]] = []
        for task_dir, pr_url in pending:
            state_data = _pr_state_via_gh(pr_url)
            satisfied, reason = _gate_satisfied_by_pr_state(state_data)
            if satisfied:
                sentinel = task_dir / GATE_SENTINELS["human_gate_pr"]
                sentinel.touch()
                print(f"  {task_dir.name}: {reason} → sentinel touched")
            else:
                next_pending.append((task_dir, pr_url))
        pending = next_pending
        if pending:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] still waiting on {len(pending)} task(s)…", file=sys.stderr)
            time.sleep(POLL_INTERVAL_SEC)

    if pending:
        print(
            f"⚠  {len(pending)} task(s) still blocked after {MAX_POLL_DURATION_SEC // 60}min — giving up. "
            "Re-run `wait-and-resume` to continue polling.",
            file=sys.stderr,
        )

    print()
    print("running resume to advance unblocked tasks…")
    return _run_pipeline(batch_dir, label="resume")


# ---------- status ----------


def cmd_status(batch_dir: Path) -> int:
    if not batch_dir.is_dir():
        print(f"error: batch directory not found: {batch_dir}", file=sys.stderr)
        return 2
    tasks = list_tasks(batch_dir)
    if not tasks:
        print(f"no tasks under {batch_dir}/tasks/")
        return 0
    for task_dir in tasks:
        state_path = task_dir / "state.json"
        if not state_path.is_file():
            print(f"  {task_dir.name}: no state.json")
            continue
        try:
            obj = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  {task_dir.name}: corrupt state.json — {e}")
            continue
        if not isinstance(obj, dict):
            print(f"  {task_dir.name}: state.json is not an object")
            continue
        print(f"  {task_dir.name}: {_summary_lines(obj)}")
        # STATIC recovery guidance only — never echo last_failure_log here. A
        # phase's raw stderr (worker/branch setup) is stored there and may carry
        # credentials (IR-004); printing it would re-introduce the leak IR-002
        # closed. The full log stays in state.json for manual inspection.
        if obj.get("last_failure_reason") or obj.get("deferred_requirements"):
            print(
                "      → fix, then `orchestrator resume`. If a codex review gate failed, run "
                '`codex login status` (re-login if expired) or set reviewer="human". '
                "Full log: state.json.last_failure_log"
            )
    return 0


# ---------- standalone review ----------


def _standalone_review_prompt(cfg: Any) -> str:
    """Prompt for a one-shot adversarial review of the current branch diff, with
    no task context. Mirrors the in-pipeline review_code prompt's read-only,
    stdout-only contract so the same reviewer adapter can run it unchanged."""
    proj = cfg.project
    return (
        "Act as an adversarial code-security reviewer. Review the changes in "
        f"`git diff {proj.base_branch}...HEAD`. Apply the review criteria in "
        ".redteam/prompts/codex/code_review.md, the project security checklist at "
        f"{proj.security_checklist}, and the project hard rules at {proj.context_file}. "
        "DO NOT write any files or touch any sentinels — output the ENTIRE review to "
        "stdout only. End with a final line `REVIEW_DECISION: APPROVED` (or "
        "CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), with IR-NNN findings above it."
    )


def _provider_family(adapter_name: str) -> str:
    """Collapse an adapter's `name` to its provider FAMILY ("claude"/"codex").

    Reviewer adapters are already named by family ("claude"/"codex"), but the
    Claude *worker* adapter is named "claude-code" — so a raw name comparison
    would read a claude worker + claude reviewer as cross-provider when it is in
    fact self-review. Normalizing both to the family is what makes the guard
    below correct. (This mirrors the worker_provider/reviewer_provider resolvers
    introduced for the in-pipeline guard; once that lands on the same branch the
    two should converge on one helper.)
    """
    return "claude" if adapter_name.startswith("claude") else adapter_name


def cmd_review(repo: Path | None = None) -> int:
    """Run the configured reviewer — fail-closed if it would collapse to the
    worker's own provider — over the current branch diff, read-only, with no
    task/state machine.

    This is the harness's cross-model review surfaced as a standalone command:
    "review my current changes with the review model." The exit code reflects the
    decision so it can gate CI — 0 = APPROVED, 1 = changes/rescue/ask (issues
    found), 2 = the reviewer itself failed (missing CLI / timeout / unparseable)
    OR the configured reviewer collapses to the worker's provider (self-review).
    Fail-closed: a failed run, and a self-review pairing, are never reported as
    an approval. The cross-provider check mirrors the in-pipeline pairing guard
    so this standalone entry point cannot become a hole that silently lets the
    same model that wrote the code review it.
    """
    rr = repo or repo_root()
    cfg = load_config(rr)
    adapter = get_reviewer_adapter({})  # {} → inherits config default reviewer
    if adapter is None:
        print(
            'error: no headless reviewer configured (reviewer="human"?). Set '
            ".redteam/config.toml [models].reviewer to a headless provider "
            '(e.g. "codex") to use `review`.',
            file=sys.stderr,
        )
        return 2
    # Fail-closed adversarial-pairing guard: refuse to "review" with the same
    # provider that the harness is configured to write with — that is self-review
    # and defeats the point of the harness, exactly what the in-pipeline guard
    # prevents. The worker resolver always returns an adapter, so a family match
    # is a genuine collapse.
    worker_family = _provider_family(get_worker_adapter({}).name)
    reviewer_family = _provider_family(adapter.name)
    if reviewer_family == worker_family:
        print(
            f"error: standalone review would be self-review — the configured reviewer and the "
            f"worker both resolve to the '{worker_family}' provider, so the code would be reviewed "
            f"by the same model that wrote it. Point .redteam/config.toml [models].reviewer at a "
            f'different provider than the implementer (e.g. reviewer="codex" with a claude-* '
            f'implementer), or set reviewer="human" for a manual review.',
            file=sys.stderr,
        )
        return 2
    result = adapter.review(
        role="review_code",
        prompt=_standalone_review_prompt(cfg),
        cwd=rr,
        target={"kind": "branch_diff", "base": cfg.project.base_branch},
    )
    raw = result["raw"]
    out_path = rr / ".redteam" / "last_review.md"
    try:
        out_path.write_text(raw, encoding="utf-8")
        saved = " (saved to .redteam/last_review.md)"
    except OSError:
        saved = ""
    print(raw)
    if result["parse_status"] != "ok":
        print(f"\n[redteam] reviewer failed (parse_status={result['parse_status']}).", file=sys.stderr)
        return 2
    print(f"\n[redteam] REVIEW_DECISION: {result['decision']}{saved}", file=sys.stderr)
    return 0 if result["decision"] == "APPROVED" else 1


USAGE = (
    "usage: orchestrator.py {start|resume|wait-and-resume|status} <batch-dir>\n"
    "       orchestrator.py review\n"
    "  start            — process every task from its current next_phase\n"
    "  resume           — same as start; convenient name to re-enter after a human gate\n"
    "  wait-and-resume  — for tasks blocked at human_gate_pr, poll GitHub via `gh pr view`\n"
    "                     and auto-touch the `pr.reviewed` sentinel once the PR is merged\n"
    "                     or closed; then run resume\n"
    "  status           — print per-task summary without running anything\n"
    "  review           — one-shot adversarial review of the current branch diff with the\n"
    "                     configured reviewer (a different provider than the worker); no batch"
)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2

    command = argv[1]

    # `review` takes no batch dir — it reviews the current branch diff.
    if command == "review":
        return cmd_review()

    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    batch_dir = Path(argv[2]).resolve()

    if command == "start":
        return cmd_start(batch_dir)
    if command == "resume":
        return cmd_resume(batch_dir)
    if command == "wait-and-resume":
        return cmd_wait_and_resume(batch_dir)
    if command == "status":
        return cmd_status(batch_dir)

    print(f"error: unknown command: {command!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
