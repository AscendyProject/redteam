## What
Extend `.redteam/workflows/benchmark.py` with the Phase 1 MVP execution layer — an
outer `run_benchmark(...)` loop, an injectable `run_one` seam, and a
deterministic metric extractor over `state["phase_telemetry"]` — so that a full
benchmark sweep (configs × tasks × repetitions) is resumable, budget-fenced,
repo-safe, and fully exercisable without ever mutating the operator's real repo
or spawning a real model in the test suite.

## Why
Follow-up to #146 Phase 1 MVP: task-001 shipped the `benchmark.toml` loader
and JSONL append/resume store; this task wires the execution layer on top —
the outer loop, the injectable `run_one` seam, and a deterministic metric
extractor over `phase_telemetry`. The hard constraint the parent goal asked
for is testability without a real model **and** repo-safety (no writes to the
real `.redteam/config.toml`, no leftover branches, no `gh`/`git push`
anywhere), so the runner uses `shutil.copytree` + `git init` on a tempcopy
with no `origin`, and runtime-rebinds `PHASE_RUNNERS["create_pr"]` to a no-op
inside a bootstrap driver — two independent layers of defense against ever
opening a PR from within a benchmark run.

## Done-when
- [ ] `.redteam/workflows/benchmark.py` exports a public metric-extractor
      callable (e.g. `extract_metrics(state: dict) -> dict`) that, given a
      completed task `state.json` dict, returns a mapping with keys
      `outcome`, `review_rounds`, `retry_count`, `rescue_count`,
      `scope_creep_count`, `wall_clock_sec`, and `claude_cost_usd` derived
      only from existing fields (`state["next_phase"]`, `state.get("deferred")`,
      `state["phase_telemetry"]`, `state.get("retries")`,
      `state.get("deferred_requirements")`) with **no new `state.json` keys**.
- [ ] Extractor rules (asserted by unit tests): `outcome == "done"` iff
      `state["next_phase"] == "done"`; `outcome == "deferred"` iff
      `state.get("deferred") is True` or `state["next_phase"] == "deferred"`;
      otherwise `outcome == "error"`. `review_rounds` = count of
      `phase_telemetry` entries with `phase == "review_code"`. `rescue_count`
      = count of `phase_telemetry` entries with `phase == "rescue"`.
      `retry_count` = `sum(state.get("retries", {}).values())` (existing
      per-phase dict; sum is the one deterministic reduction the extractor pins).
      `scope_creep_count` = count of entries in
      `state.get("deferred_requirements", [])` whose `reason` (string) starts
      with a floor-trip marker; when no stable predicate matches (schema
      variance), the extractor returns `0` — it does NOT invent a new
      `state.json` key. The unit-test fixture uses a shape actually emitted by
      the engine (see Risks for the reference to real code paths).
      `wall_clock_sec` = `sum((e.get("duration_sec") or 0.0) for e in
      state.get("phase_telemetry", []))`.
      `claude_cost_usd` = `sum(e["cost_usd"] for e in phase_telemetry if
      e.get("provider") == "claude" and e.get("cost_usd") is not None)`
      **or `None`** when no Claude entry with a cost exists (never fabricated
      from Codex-only runs).
- [ ] `.redteam/workflows/benchmark.py` exports `run_one(set_root, config_name,
      task_id, repetition, *, config_overrides, workspace)` returning a record
      dict matching the task-001 `BenchmarkRecord` schema
      (`schema_version=1`, `config`, `task`, `repetition`, `outcome`,
      `review_rounds`, `retry_count`, `rescue_count`, `scope_creep_count`,
      `wall_clock_sec`, `claude_cost_usd`, `started_at`, `finished_at`).
- [ ] `run_one` measures `wall_clock_sec` via `time.monotonic()` around the
      subprocess dispatch (NOT re-derived only from telemetry sums), and
      records `started_at` / `finished_at` as ISO-8601 UTC strings.
- [ ] `run_one` follows the isolation strategy above: `shutil.copytree` snapshot
      → `git init` on `main` with **no `origin` remote** → merged
      `.redteam/config.toml` written under the tempcopy only → batch seeded
      under the tempcopy → bootstrap driver written under the tempcopy that
      runtime-rebinds `orchestrator.PHASE_RUNNERS["create_pr"]` to a no-op
      that sets `next_phase = "done"` and returns `approved` → subprocess with
      `cwd=<tempcopy>` and `sys.executable` as the interpreter → state.json
      read from the tempcopy → tempcopy auto-deleted via `TemporaryDirectory`.
- [ ] After `run_one` returns (success OR exception), the tempcopy is deleted
      by the `TemporaryDirectory` context manager; the operator's real
      `.redteam/config.toml`, `.git`, and worktree are unchanged (no new
      branches, no dirty files) — asserted by a hermetic test that stubs
      `subprocess.run` and reads the real config bytes before and after.
- [ ] `.redteam/workflows/benchmark.py` exports
      `run_benchmark(set_root: Path, *, dry_run: bool = False,
      run_one=<default>) -> int` that (a) loads the set via
      `load_benchmark_set`, (b) computes the plan
      `[(config, task, rep) for config in set.configs for task in
      set.task_ids for rep in 1..set.repetitions]`, (c) subtracts triples
      already in `completed_triples(load_records(...))`, (d) iterates the
      remaining plan **in that order**.
- [ ] Dry-run branch: when `dry_run=True`, prints to stdout the planned run
      count, the skipped-because-already-done count, and a cost estimate =
      `mean(claude_cost_usd for prior records where not None) * planned_count`
      or the literal string `"unknown"` when no prior Claude-cost data
      exists; **`run_one` is never called** and the function returns `0`.
- [ ] Budget check (PER-INVOCATION): before each dispatch, compute
      `accumulated = sum((r["claude_cost_usd"] or 0.0) for r in records
      appended THIS invocation)`; when `set.budget_usd is not None` AND
      `accumulated + estimated_next >= set.budget_usd`, abort **before**
      calling `run_one`, print a clear stderr line naming
      `config`/`task`/`repetition`, `accumulated`, `estimate`, `budget`,
      and return exit code `3`. `estimated_next` = same mean-of-prior-Claude
      costs used by dry-run, or `0.0` when unknown (unknown estimate does
      NOT by itself trigger an abort).
- [ ] Budget scope is documented in code and enforced by tests: prior JSONL
      records (already spent and skipped by resume) do **not** count toward
      the in-invocation accumulated budget — a test pre-seeds `$5.00` of
      historical records and asserts the abort count is identical to the
      no-history case.
- [ ] When `run_one` raises, the loop catches the exception, appends an
      `outcome="error"` record populating at minimum `config`, `task`,
      `repetition`, `started_at`, `finished_at`, `outcome`, then continues
      to the next triple. `run_benchmark` returns `0` on normal completion
      (including after error-continues); non-zero only on budget abort.
- [ ] `append_record(...)` is called immediately after every `run_one`
      return (success OR error record) so a `Ctrl-C` between runs resumes
      cleanly on the next invocation.
- [ ] `.redteam/workflows/benchmark.py` source contains **no** literal
      references to `"gh "`, `"git push"`, `"pr create"`, `"--force"`, or
      any auto-merge language, and does **not** import `phase_runners.create_pr`
      or the top-level `create_pr` module. Asserted by a static grep-style test
      in the new test file. (The bootstrap-driver string that runtime-rebinds
      `PHASE_RUNNERS["create_pr"]` is a bracket-key literal `"create_pr"`,
      which is fine — the grep patterns are the shell / call phrases above,
      not the phase key.)
- [ ] `.redteam/workflows/benchmark.py` imports remain stdlib-only
      (`tomllib`, `json`, `os`, `shutil`, `statistics`, `subprocess`, `sys`,
      `pathlib`, `tempfile`, `time`, `datetime`, `dataclasses`, `typing`);
      no pip dependency is added.
- [ ] `.redteam/workflows/orchestrator.py`, `.redteam/workflows/config.py`,
      every file under `.redteam/workflows/phase_runners/`, and every file
      under `.redteam/workflows/adapters/` are unchanged
      (`git diff --name-only main...HEAD` lists only files under
      `.redteam/workflows/benchmark.py` and
      `.redteam/tests/test_benchmark_runner.py`).
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff + ruff format + full
      pytest, including `test_agents_generic_prompts.py` and the new tests).

## Verification
- Tests: test_run_benchmark_happy_path_8_records, test_run_benchmark_resume_skips_completed, test_run_benchmark_dry_run_with_cost_estimate, test_run_benchmark_dry_run_unknown_cost, test_run_benchmark_budget_abort_mid_run, test_run_benchmark_budget_abort_stderr_names_refused_triple, test_run_benchmark_budget_scope_per_invocation, test_run_benchmark_unknown_estimate_never_aborts, test_run_benchmark_run_one_raises_continues, test_run_benchmark_codex_only_cost_passthrough, test_extract_metrics_done_outcome, test_extract_metrics_deferred_via_flag, test_extract_metrics_deferred_via_next_phase, test_extract_metrics_error_outcome, test_extract_metrics_review_rounds_and_rescue_count, test_extract_metrics_claude_cost_sums_only_claude_entries, test_extract_metrics_claude_cost_none_when_codex_only, test_extract_metrics_wall_clock_sec_sums_duration_with_none, test_extract_metrics_empty_telemetry, test_extract_metrics_scope_creep_count_returns_zero, test_extract_metrics_scope_creep_count_counts_floor_trips, test_extract_metrics_retry_count_sums_retries_dict, test_benchmark_source_no_unsafe_literals, test_benchmark_source_no_create_pr_import, test_run_one_real_config_untouched, test_bootstrap_driver_contains_phase_runners_rebind, test_bootstrap_driver_contains_sys_exit_cmd_start, test_bootstrap_driver_no_unsafe_literals
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff scoped to `.redteam/workflows/benchmark.py` (+439 lines) and `.redteam/tests/test_benchmark_runner.py` (+744 lines, 28 hermetic tests); no engine, adapter, or `phase_runners/*` files touched.
- Reviewer (Codex) resolved both raised majors: IR-001 (budget scope) — the per-invocation test now seeds 50 historical records worth `$5.00` and asserts the abort count remains 5, matching the no-history case; IR-002 (`scope_creep_count`) — extractor now counts `deferred_requirements[].feedback` entries whose text starts with the two floor-trip prefixes actually emitted by `implement.py` (tracked-WIP floor and cross-run trust-root floor).
- Imports remain stdlib-only; subprocess dispatch uses arg-list form with `encoding="utf-8"`; the `PHASE_RUNNERS["create_pr"]` runtime-rebind lives only inside the tempcopy bootstrap driver.
- Static grep test confirms `benchmark.py` source contains no `gh`, `git push`, `pr create`, `--force`, or auto-merge language, and does not import the real `create_pr` module.
- `verification.log` present; `state.verification.last_exit_code == 0`; reported run is `819 passed`.
- Final decision: `REVIEW_DECISION: APPROVED`.

## Generated by
redteam / batch benchmark-phase1 / task task-002-benchmark-runner
