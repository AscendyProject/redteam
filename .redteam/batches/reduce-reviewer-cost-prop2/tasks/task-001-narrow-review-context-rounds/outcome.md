# Outcome — Narrow round-over-round reviewer context for carried-over findings (#92 Proposal 2)

## Goal
On `review_code` rounds 2+ in agent-pair mode, hand the reviewer the incremental
delta since the previously-reviewed revision plus the carried-over open
`review_items` — instead of re-embedding the full accumulated
`git diff <base>...HEAD` every round — while keeping the adversarial-fidelity
guarantees (new changes in the delta are always fully reviewed; carried-over
findings are still adjudicated; any uncertainty fails back to today's full-diff
prompt; reviewer output contract and orchestrator-owned bookkeeping unchanged).

## Done-when
- [ ] In `.redteam/workflows/phase_runners/review_code.py`, the agent-pair branch
      of `run()` reads `state.get("last_reviewed_rev")` (top-level state key,
      `str`; absence treated as "first round"). The new key is read-only on
      legacy state — its absence MUST take the full-review path and MUST NOT
      raise `KeyError` or any other exception.
- [ ] After `review_with_fallback` returns inside the agent-pair branch, the
      runner sets `state["last_reviewed_rev"] = git_rev_parse("HEAD",
      repo_root())` ONLY when BOTH `result["parse_status"] == "ok"` AND
      `result["decision"] in {"APPROVED", "CHANGES_REQUESTED",
      "RESCUE_REQUIRED", "ASK_USER"}`. The runner MUST NOT write
      `last_reviewed_rev` on the `MANUAL_REQUIRED` branch nor on the
      `parse_status != "ok"` fail-closed branch (so a manual-pending or
      malformed-review state never poisons the next round's narrowing).
- [ ] If the `git rev-parse HEAD` capture raises `RuntimeError`, the runner
      catches it and leaves `state["last_reviewed_rev"]` untouched (the next
      round simply uses the full-diff path) — the captured exception MUST NOT
      propagate out of `run()`.
- [ ] Narrowing precondition (ALL must be true to take the narrowed path on a
      given round): (a) `state.get("last_reviewed_rev")` is a non-empty `str`;
      (b) `state.get("review_items")` is a list containing at least one `dict`
      with `status == "open"`; (c) `git merge-base --is-ancestor <prior> HEAD`
      returns exit code 0; (d) `git diff <prior>...HEAD` exits 0 with non-empty
      stdout. If ANY precondition fails, the runner takes the full-diff path
      (no exception, no log mutation beyond what today does).
- [ ] On the narrowed path, the prompt passed to `review_with_fallback` is
      built by a new private helper
      `_narrowed_code_review_prompt(task_dir, base_branch, prior_rev,
      open_items)` in `review_code.py`. Its output is byte-deterministic for a
      given input and contains, in this order:
        1. `Act as an adversarial code-security reviewer for the implementation
           of the task at {task_dir}/. Review `git diff
           {prior_rev}...HEAD`.` (the incremental delta — full adversarial pass
           on everything that changed since the previously-reviewed revision).
        2. A line `Pinned base for the PR remains {base_branch}; the narrowed
           diff above is the round-over-round delta, not a replacement for the
           base.`
        3. A `Carried-over open findings (adjudicate each as resolved or still
           open):` section, followed by one line per open item formatted
           `- {id} severity:{severity} status:{status} — {summary}` (fields
           drawn directly from `state["review_items"]`).
        4. The same task inputs the full prompt names today: `{task_dir}/
           outcome.md`, `{task_dir}/plan_review.md`,
           `{task_dir}/impl_diff.patch`.
        5. The same project references the full prompt names today:
           `.redteam/prompts/codex/code_review.md`, `proj.security_checklist`,
           `proj.context_file`.
        6. The same stdout-only / no-file-writes / no-sentinel-touch language
           the full prompt uses today.
        7. The same closing line: `End with a final line `REVIEW_DECISION:
           APPROVED` (or CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), with
           IR-NNN findings above it.`
- [ ] On the full-diff path (first round OR any failed precondition), the
      runner calls the existing `_code_review_prompt(task_dir, base_branch)`
      byte-identically to today; no edits to `_code_review_prompt` are required
      beyond what is necessary to keep `test_headless_prompts_forbid_writes`
      green.
- [ ] `review_with_fallback` is invoked exactly once per round with
      `role="review_code"`, `cwd=repo_root()`,
      `target={"kind": "branch_diff", "base": pinned_base_branch(state, rr)}`
      (the pinned base, #91) — on BOTH the narrowed and the full path. The
      `target.base` MUST remain the pinned base; narrowing lives in the
      prompt only.
- [ ] The two new git probe helpers live as private module-level functions in
      `review_code.py` (NOT in `_base.py`): `_is_ancestor(prior: str, repo:
      Path) -> bool` (wraps `subprocess.run(["git", "merge-base",
      "--is-ancestor", prior, "HEAD"], ...)`, returncode 0 → True, anything
      else → False, never raises) and `_incremental_diff_nonempty(prior: str,
      repo: Path) -> bool` (wraps `subprocess.run(["git", "diff",
      f"{prior}...HEAD"], ...)`, returncode 0 with non-empty stdout → True,
      anything else → False, never raises). Both use shell-free arg lists and
      `encoding="utf-8"`, matching the discipline in `_base.py`.
- [ ] The fail-closed branches in the runner (`result["parse_status"] !=
      "ok"` → `PhaseResult(status="error", ...)`, `MANUAL_REQUIRED` →
      `PhaseResult(status="manual_required", ...)`, the
      `decision in {APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER}`
      mapping to `PhaseResult.status`, and the `code_review.md`
      `write_text(..., encoding="utf-8")` artifact write) remain functionally
      identical on the narrowed path.
- [ ] The non-agent-pair / TDD sub-agent reviewer path (`run()` tail,
      `state.get("mode") != "agent-pair"`, the `impl_diff.patch`-driven fresh
      reviewer) is NOT modified — it keeps today's full-diff behavior
      unchanged.
- [ ] No new state key is added beyond `last_reviewed_rev`. The orchestrator's
      `_sync_review_items` call at `orchestrator.py` ~line 1457 and the
      `_close_phase_review_items` call at ~1493 are unchanged in signature,
      site, or behavior; the new prompt is built only from data the runner
      already has access to (`state["review_items"]`).
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff check + ruff format
      --check + full pytest under `.redteam/tests/`), with no existing test
      regressing.
- [ ] A new test file at `.redteam/tests/test_review_code_narrow_context.py`
      (matching `test_*.py`) covers the first-round-full-diff,
      subsequent-round-narrowed, new-issue-still-caught, fail-safe-fallback,
      and contract-intact behaviors below using monkeypatched
      `get_reviewer_adapter`, `review_with_fallback`, `compute_repo_diff`,
      `repo_root`, `git_rev_parse`, and the two new private probe helpers — no
      `codex` / `claude` subprocess invocations and no network or remote git
      I/O.

## Out of scope
- **#92 Proposal 1** (deterministic verify pre-gate before `review_code`) —
  already satisfied by today's architecture; do NOT re-implement.
- **#92 Proposals 3 / 4 / 5** (round-staged model tiering; SAST/semgrep
  offload; prompt caching + hard ceilings).
- Narrowing the `plan_review` phase's reviewer context — this task is
  `review_code` only.
- Changing the `ReviewerAdapter` / `ReviewTarget` protocol shape, the
  `review_with_fallback` ladder (#37), the manual sentinel path, or the
  `REVIEW_DECISION` parsing in `parse_review_decision`.
- Moving the PR base or `target.base` off the pinned `base_branch` (#91) —
  the incremental ref lives in the prompt only.
- The non-agent-pair / TDD sub-agent reviewer path at the tail of
  `review_code.py` (the `impl_diff.patch`-based fresh reviewer): explicitly
  left on the full-diff path; no mirror.
- Promoting `_is_ancestor` / `_incremental_diff_nonempty` to `_base.py` (they
  are used only by `review_code.py`).
- Persisting any additional revision history (`reviewed_rev_history`,
  per-round audit, etc.). Only the single most recent `last_reviewed_rev` is
  kept.
- Adding any pip dependency to the engine.

## Affected files
- `.redteam/workflows/phase_runners/review_code.py` — add
  `_narrowed_code_review_prompt`, `_is_ancestor`,
  `_incremental_diff_nonempty`; in the agent-pair branch of `run()`, choose
  narrowed-vs-full prompt before invoking `review_with_fallback`, then capture
  `HEAD` into `state["last_reviewed_rev"]` on a successful parsed decision.
- `(new) .redteam/tests/test_review_code_narrow_context.py` — deterministic
  tests (monkeypatched reviewer adapter + git helpers) for the five behaviors
  listed under `## Verification` below. The test-author phase chooses exact
  test function names.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  --check + pytest over `.redteam/tests/`) must pass.
- `.redteam/tests/test_reviewer_adapter.py` — pins the existing
  `_code_review_prompt` / agent-pair runner contract this task must preserve
  (esp. `test_review_code_runner_uses_adapter_in_agent_pair`,
  `test_headless_prompts_forbid_writes`,
  `test_runner_fails_closed_on_unparseable_with_stray_decision`).
- `.redteam/tests/test_reviewer_fallback.py` — `review_with_fallback` ladder
  semantics, untouched by this change.
- `.redteam/tests/test_base_branch_pin.py` and
  `.redteam/tests/test_pinned_base_freeze_guard.py` — the `pinned_base_branch`
  invariant remains in force for `target.base` on every round and for the PR
  base.

### To be created (the test-writing phase will define exact test function names)
Tests under `.redteam/tests/` (file naming `test_*.py`), targeting the
agent-pair branch of `phase_runners.review_code.run`, covering:
1. **First round → full diff.** With no `last_reviewed_rev` in state, the
   prompt passed to `review_with_fallback` is the byte-identical output of
   `_code_review_prompt(task_dir, base_branch)` (today's full-diff prompt);
   after the round completes with a parsed `ok` decision,
   `state["last_reviewed_rev"]` equals the value returned by the monkeypatched
   `git_rev_parse("HEAD", repo)`.
2. **Subsequent round → narrowed.** With `state["last_reviewed_rev"]` set to a
   valid SHA, `state["review_items"]` containing at least one `dict` with
   `status == "open"`, and both probe helpers returning `True`, the prompt
   passed to `review_with_fallback` contains the literal substring
   ``Review `git diff <prior>...HEAD`.`` AND each open item's `id` + `severity`
   + `status` + `summary`. The prompt MUST NOT contain ``git diff
   <pinned_base>...HEAD`` (assert the full accumulated range is not
   re-embedded). `target` still equals `{"kind": "branch_diff", "base":
   <pinned_base>}`.
3. **New issue in the delta is still caught.** With the narrowed prompt in
   play, a monkeypatched `review_with_fallback` that returns
   `{"decision": "CHANGES_REQUESTED", "raw": "IR-002 severity:major
   status:open ...\nREVIEW_DECISION: CHANGES_REQUESTED", "parse_status":
   "ok"}` produces `PhaseResult.status == "changes_requested"` and the runner's
   `log` (which the orchestrator forwards to `_sync_review_items`) contains
   the new `IR-002` line — exercise `_sync_review_items` on that log directly
   and assert the new item lands with `status == "open"` and
   `carry_over_count == 1`.
4. **Fail-safe fallback.** Each of the following independently forces the
   runner onto the full-diff prompt (byte-identical to
   `_code_review_prompt(task_dir, base_branch)`) without raising:
   (a) `last_reviewed_rev` absent from state;
   (b) `last_reviewed_rev` present but `_is_ancestor` returns `False`
       (non-ancestor / rebase-amend case);
   (c) `_incremental_diff_nonempty` returns `False` (empty or failed diff);
   (d) `state["review_items"]` empty or all items have `status != "open"`.
5. **Contract intact.** On the narrowed path:
   (a) a `MANUAL_REQUIRED` result yields `PhaseResult.status ==
       "manual_required"` and `state["last_reviewed_rev"]` is NOT mutated;
   (b) a `parse_status != "ok"` result yields `PhaseResult.status == "error"`
       and `state["last_reviewed_rev"]` is NOT mutated;
   (c) on a successful `ok` decision, `code_review.md` is written under
       `task_dir` with `encoding="utf-8"`;
   (d) `_sync_review_items` and `_close_phase_review_items` (called by the
       orchestrator, not the runner) operate identically on the runner's
       output.

Test scaffolding restrictions: monkeypatch `get_reviewer_adapter`,
`review_with_fallback`, `compute_repo_diff`, `repo_root`, `git_rev_parse`, and
the two new private probes (`_is_ancestor`,
`_incremental_diff_nonempty`); do NOT spawn `codex` / `claude` subprocesses
and do NOT touch a real remote.

## Risks
- **Adversarial blind spot (highest).** Any narrowing that drops a part of the
  round's actual changes lets a fix-introduced bug escape review. The
  invariant is: the reviewer's diff reference on the narrowed path is
  `git diff <last_reviewed_rev>...HEAD`, which by git semantics covers every
  change committed (or staged & committed by the implement phase's
  `commit_paths`) since the previously-reviewed revision, including new
  files anywhere in the tree (mirroring the #82 "complete faithful view"
  point). Test 3 is the regression guard. Note that today's commit step
  stages and commits new files before review, so a staged-but-uncommitted
  delta is not a hole this design introduces.
- **Stale prior ref after rebase/amend.** A rebase or amend mid-task can
  leave `last_reviewed_rev` no longer reachable from `HEAD`. The
  `_is_ancestor` check is the guard; on `False` the runner falls back to
  the full-diff prompt. Test 4(b) covers this.
- **Resume / legacy state.** A task mid-flight from before this change has
  no `last_reviewed_rev` key. `state.get("last_reviewed_rev")` returns
  `None`, the runner takes the full path, and the round writes the key on
  exit — no migration required. Test 4(a) covers this.
- **`HEAD` capture failure.** `git_rev_parse("HEAD", ...)` is wrapped in a
  `try/except RuntimeError` that swallows the error and leaves
  `last_reviewed_rev` untouched (next round transparently uses the full
  path). No new failure mode is introduced into `run()`'s return path.
- **Dogfooded change risk.** This task changes the same `review_code` phase
  that will review the change. Operational note only — the #112 fix (durable
  baseline pin) is on `main` and removes the prior dead-end; no design
  implication for this task.
- **Exact line numbers in the brief may drift.** All references in this
  outcome are by symbol (`_code_review_prompt`, `run`, `review_with_fallback`,
  `_sync_review_items`, `pinned_base_branch`) — the implementer must locate
  by symbol, not by line number.
