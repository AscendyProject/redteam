# Slice B: goal→task decomposer + cross-provider decomposition review

Implements **Slice B** (Piece 2) of the goal-mode design (#94). The accepted umbrella
design is `docs/decisions/2026-06-27-goal-mode-design.md` (Piece 2 + Open decisions #2,
#3, #5); it currently lives on the not-yet-merged branch `docs/goal-mode-design-94`
(PR #110), so it may be absent on this branch — treat **this brief as authoritative**
(it faithfully encodes Piece 2). This slice **stacks on Slice C** (PR #113, branch
`redteam/task-002-slice-c-ceilings`), which made `goal.json` ceilings load-bearing and
added the goal-level done-criterion; Slice A (PR #111) added the single-parent manifest
+ layered scheduler + the pin/freeze/branch invariants. **Slice B is the last piece and
must not ship without C** (C is done). The decomposer generates the task forest that the
ceiling bounds and the scheduler runs — an untrusted-agent-generated artifact gating real
work — so this is a **safety-boundary change and goes through `plan_review` before any
code.**

## Goal
A human writes a one-file **`goal.md`** at a batch root. A **decomposer agent** turns it
into a **single-parent `goal.json` manifest + one `tasks/<id>/input.md` brief per task**.
Before any task is seeded or run, the generated decomposition goes through a
**cross-provider adversarial decomposition review** (mirrors per-task `plan_review`, one
level up); a REJECTED decomposition **fails closed** — no task state seeded, no task run.
The generated manifest is **never trusted on its own**: it flows through Slice A's
existing `_load_goal_manifest` fail-closed validation (single-parent, no cycle, no
unknown/self ref) AND Slice C's `max_tasks` ceiling, exactly as a hand-fed manifest does.
Absent `goal.md`, behavior is **byte-for-byte unchanged** (a hand-fed `goal.json` runs
exactly as under A/C). Decomposition completion is the **validated, reviewed manifest +
briefs existing** — it does NOT run tasks or merge anything (the draft-PR stack and the
human checkpoint are unchanged downstream).

## What to build
1. **Decomposer agent + prompt (project-agnostic).** A new generic planning agent that
   reads `goal.md` (+ the ceiling `max_tasks`) and emits:
   - `goal.json` — a **single-parent** manifest (the schema Slice A consumes:
     `{"goal", "ceilings", "tasks": {"<id>": {"depends_on": [<=1 parent]}}}`),
   - `tasks/<id>/input.md` — one clean-boundary brief per task, in the same shape the
     `outcome-planner` already consumes downstream.
   The prompt is **stack/project-agnostic** (it must reason about ANY `goal.md`, embed no
   project- or stack-specific fingerprints) and lives beside the other prompts
   (`.redteam/prompts/codex/`); the agent skeleton goes in `.claude/agents/` as a 7th
   generic sub-agent. **`test_agents_generic_prompts.py` must stay green.**
   - **Single-parent only (v1).** The decomposer cannot express multi-parent. If a natural
     decomposition needs a task to depend on ≥2 others, it **serializes into a chain** or
     **stops and asks** (emits a clear "cannot decompose within single-parent v1" signal
     and fails closed — **no partial forest is run**).
   - **Respect the ceiling.** The decomposer is told `max_tasks` and must not exceed it;
     Slice A+C's load-time validation is the **hard backstop** (a generated manifest with
     > `max_tasks` tasks, ≥2 deps, a cycle, an unknown/self ref aborts fail-closed exactly
     as a hand-fed one would — reuse that path, do not add a second weaker one).
2. **Cross-provider decomposition review (the adversarial gate).** After generation and
   **before any task seeding/run**, the manifest + the generated briefs go through a
   cross-provider adversarial review that mirrors per-task `plan_review` one level up.
   **Reuse the existing reviewer transport** (the review adapter + the #37 fallback
   ladder) — do NOT invent a new transport (the sub-agent reviewer path was rejected; see
   `docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`). The review emits a
   decision on its final line (same `REVIEW_DECISION:` convention). **Fail closed:** a
   non-APPROVED decision aborts the goal batch (seed no task state, run no task) and
   surfaces the review artifact; APPROVED lets the existing Slice A/C scheduler run the
   validated stack unchanged.
3. **Entry-point wiring (operator-driven, conservative).** Add a decomposition step that
   **generates + reviews, then STOPS before running any task** so the human can inspect
   the generated stack (this matches redteam's "human checkpoint" identity). Proposed:
   a distinct `orchestrator decompose <batch>` subcommand that produces the reviewed
   `goal.json` + briefs; the operator then runs `start`/`resume` to execute the validated
   stack. **Let `plan_review` settle** the exact entry-point shape (separate `decompose`
   subcommand vs a pre-step inside `start` that runs once when `goal.md` is present and
   `goal.json` is absent) and whether Slice B should itself sub-slice (decomposer-gen vs
   decomposition-review) if it is too large for one PR.

## Constraints
- **Engine stays project-agnostic; stdlib-only; zero runtime deps.** No project/stack
  fingerprints in `.redteam/workflows/` or the decomposer prompt/agent skeleton. The
  decomposer reasons about an arbitrary `goal.md`.
- **Backward compatible:** absent `goal.md`, `decompose` is never triggered and
  `process_batch`/`_run_pipeline` behave exactly as under A/C (a hand-fed `goal.json`
  still runs identically; existing tests stay green).
- **Untrusted-output discipline:** the agent-generated manifest MUST flow through Slice
  A's `_load_goal_manifest` fail-closed validation and Slice C's `max_tasks` ceiling — the
  decomposition review is an **additional** adversarial gate, **not a substitute** for
  load-time validation. Do not add a second, weaker validation path.
- **Preserve every Slice A/C invariant** (single-parent forest, pin-before-branch, freeze
  guard in `pinned_base_branch`, `blocked_on_dependency` cascade, `max_tasks` enforcement,
  goal-done criterion). Do NOT touch `_ensure_task_branch`, the `base_branch` pin, the
  freeze guard, the scheduler, or the ceiling enforcement — **reuse** them.
- **No auto-merge, no auto re-plan (decision #3):** a REJECTED decomposition is surfaced;
  the human edits `goal.md` and re-runs. Re-running `decompose` must **not silently
  clobber** a human-edited `goal.json`/`input.md` (fail closed or require an explicit
  `--force`).
- Match existing orchestrator/phase-runner style; minimum code.

## Out of scope
- **Multi-parent DAGs** (v1 single-parent; the decomposer serializes-or-stops).
- **Auto re-plan** on a bad decomposition (human edits `goal.md` + re-runs — decision #3).
- **Token / wall-clock ceilings** (Slice C left them parse-tolerate; not enforced here).
- Any change to `_ensure_task_branch`, the `base_branch` pin, the freeze guard, the
  layered scheduler, or `max_tasks` enforcement (Slice A/C, already on this branch).
- The **per-task `plan_review`** — it still runs per task downstream, unchanged; this slice
  adds the review one level UP (of the decomposition itself), not a replacement.

## Affected files
- `.redteam/workflows/orchestrator.py` — decomposition entry (subcommand or `start`
  pre-step): invoke the decomposer agent, write `goal.json` + `tasks/<id>/input.md`, run
  the decomposition review, **fail closed** on a non-APPROVED decision, and stop before any
  task run; on APPROVED hand off to the existing scheduler.
- `.redteam/workflows/phase_runners/` — likely a new `decompose.py` (mirrors the
  plan/review runner structure) reusing the reviewer adapter; or thin glue if the existing
  runners suffice (planner decides).
- `.redteam/prompts/codex/` — new generic `goal-decomposer` prompt.
- `.claude/agents/` — new generic `goal-decomposer` agent skeleton (keep
  `test_agents_generic_prompts.py` green).
- Tests under `.redteam/tests/` — decomposition validation + review-gate invariant tests.

## Verification
- `bash .redteam/scripts/verify.sh` stays green (ruff check + ruff format --check +
  pytest), including `test_agents_generic_prompts.py`, `test_install.py`, and Slice A/C's
  `test_goal_manifest_validation.py` / `test_goal_dag_scheduler.py` /
  `test_goal_ceilings_enforcement.py` / `test_goal_done_criterion.py`.
- New tests assert the real invariants (not just "the agent was called"):
  - a generated manifest that violates single-parent / has a cycle / unknown or self ref /
    exceeds `max_tasks` aborts the WHOLE batch fail-closed (seeds NO state, runs NO task) —
    i.e. decomposer output is validated by the SAME Slice A+C path;
  - a decomposition review that returns non-APPROVED aborts fail-closed (no task state
    seeded, no task run) and the review artifact is surfaced;
  - an APPROVED decomposition lets the scheduler run the validated stack;
  - absent `goal.md` → byte-for-byte unchanged (a hand-fed `goal.json` runs as under A/C;
    `decompose` is never triggered);
  - re-running `decompose` does not silently clobber a human-edited `goal.json`/`input.md`
    (fail closed / `--force` required);
  - the decomposer prompt + agent skeleton are project-agnostic
    (`test_agents_generic_prompts.py` stays green).
- The agent invocation itself is mocked/stubbed in tests (no live model call); tests assert
  the orchestration contract (validation, fail-closed gate, hand-off), not model output.

## Risks
- **Untrusted agent output.** The decomposer is a model; its `goal.json` is adversarial
  input to the engine. It MUST go through Slice A's `_load_goal_manifest` fail-closed
  validation + Slice C's `max_tasks` — reuse that exact path; the decomposition review does
  NOT replace it. A second, weaker validation path is the trap to avoid.
- **Fail-closed everywhere a decision is uncertain.** Single-parent serialize-or-stop, a
  rejected review, a re-run over human-edited files, a malformed agent output — every one
  must fail closed (no partial forest seeded/run), never "best effort."
- **Project-agnosticism.** The prompt/skeleton must embed no project/stack fingerprints
  (`test_agents_generic_prompts.py` guards this) — the decomposer reasons about ANY goal.
- **Reviewer transport reuse.** Use the existing review adapter + #37 fallback ladder; do
  NOT revive the rejected sub-agent reviewer transport
  (`docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`).
- **Idempotency.** `decompose` is a generative step; guard against clobbering prior
  (possibly human-edited) output — decision #3 is human-edits-then-reruns, so do not
  auto-overwrite without an explicit force.
