# Outcome — `benchmark` runner (loop + `run_one` seam + budget + safety)

## Goal
Extend `.redteam/workflows/benchmark.py` with the Phase 1 MVP execution layer — an
outer `run_benchmark(...)` loop, an injectable `run_one` seam, and a
deterministic metric extractor over `state["phase_telemetry"]` — so that a full
benchmark sweep (configs × tasks × repetitions) is resumable, budget-fenced,
repo-safe, and fully exercisable without ever mutating the operator's real repo
or spawning a real model in the test suite.

## Isolation strategy (addresses plan_review PR-001, PR-002)

The prior plan claimed `run_one` could "thread a temp repo root through the
existing `load_config(repo_root)` seam" — that seam does not exist. `process_task`
pins `repo = repo_root()` internally (`orchestrator.py:1055`), `_run_pipeline`
takes only `batch_dir` (`orchestrator.py:1818`), `repo_root()` derives from
`__file__` (`phase_runners/_base.py:120`), and several phase runners call
`load_config(repo_root())` directly. There is no in-process seam.

This plan therefore replaces that design with **subprocess + tempcopy isolation**
that requires **zero engine changes** (no edits to `orchestrator.py`, `config.py`,
`phase_runners/*`, or `adapters/*`):

1. Snapshot the harness tree with `shutil.copytree(repo_root(), tempcopy,
   ignore=shutil.ignore_patterns(".git", "venv", ".venv", "__pycache__",
   ".redteam/batches", "results", "results.jsonl", "*.egg-info"))` into a temp
   directory created via `tempfile.TemporaryDirectory()`.
2. `git init` the tempcopy on branch `main`, set local `user.name`/`user.email`,
   and seed one initial commit — **no `origin` remote is configured**. The child
   pipeline needs `git` for branching / diffs; the absence of `origin` is
   defense-in-depth for PR-002 (see below).
3. Write the merged `.redteam/config.toml` (base config's non-`models` sections
   preserved verbatim; `[models]` role keys overwritten with `config_overrides`)
   into `<tempcopy>/.redteam/config.toml`. **The real `.redteam/config.toml` is
   never opened for write.**
4. Seed the batch: copy `<set_root>/tasks/<task_id>/input.md` into
   `<tempcopy>/.redteam/batches/bench-<config>-<task>-<rep>/tasks/<task_id>/input.md`.
5. Write a **bootstrap driver** `<tempcopy>/_bench_driver.py` (a hardcoded
   multi-line string inside `benchmark.py`) that:
   - Inserts `<tempcopy>/.redteam/workflows` into `sys.path`.
   - `import orchestrator` — from the tempcopy, so `repo_root()` derived from
     `__file__` resolves to `<tempcopy>`.
   - Rebinds `orchestrator.PHASE_RUNNERS["create_pr"]` to an inline no-op that
     writes `state["next_phase"] = "done"` and returns an `approved` `PhaseResult`.
     `process_task` looks up `PHASE_RUNNERS.get(phase)` per phase step
     (`orchestrator.py:1364`), so the runtime rebind takes effect for the run.
   - Calls `sys.exit(orchestrator.cmd_start(pathlib.Path(<batch_dir>)))`.
6. `subprocess.run([sys.executable, "-u", str(<tempcopy>/"_bench_driver.py")],
   cwd=<tempcopy>, check=False, encoding="utf-8", timeout=<bounded>)`.
7. On return, read `<tempcopy>/.redteam/batches/.../tasks/<task_id>/state.json`,
   apply the extractor, build the record dict, return.
8. On any exception or normal return, the `TemporaryDirectory` context manager
   deletes the tempcopy; the real `.redteam/config.toml` and worktree are
   unchanged.

The PR-002 defense is **two layers deep**: (a) `PHASE_RUNNERS["create_pr"]` is
runtime-rebound to a no-op in the child, so the real `create_pr` runner is never
invoked; (b) even if (a) were bypassed, the tempcopy has no `origin` remote, so
`create_pr`'s preflight `git remote get-url --push origin` (`create_pr.py:82`)
fails closed and defers before any `gh` / `git push` runs. `benchmark.py` source
itself contains no `gh`, no `git push`, no `--force`, no `pr create`, and no
call to the real `create_pr` module — asserted by a static grep test.

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

## Out of scope
- Wiring `benchmark` / `benchmark-report` subcommands into
  `orchestrator.main`, `USAGE`, or any CLI surface (task-003).
- Aggregation, markdown-diff report, cross-config rendering (task-003).
- Any modification of `orchestrator.py`, `config.py`, `phase_runners/*`, or
  `adapters/*` (pipeline internals stay untouched this task).
- Adding or renaming any `state.json` / `phase_telemetry` key — the extractor
  reads only what task-001 (#150) already stores.
- Matrix expansion, Pareto frontier, `recommend-models`, LLM-judge scorers,
  sqlite mirror, external eval-platform export (Phase 2+).
- Historical-batch conversion (`.redteam/batches/*` → benchmark set).
- Full **git-worktree** isolation of the child pipeline (the sequential
  `shutil.copytree` + `git init` approach is the MVP contract; a
  `git worktree add`-based variant is a permitted future refinement, not
  required here).
- Cross-invocation cumulative budget accounting (Phase 2 refinement — MVP is
  per-invocation and the test enforces that).
- Cross-run fsync/locking or partial-write recovery on `results.jsonl`.
- Any end-to-end integration test that actually spawns a real subprocess or
  a real model (the whole loop must be exercisable via stub `run_one`;
  subprocess dispatch is exercised only via `monkeypatch.setattr(benchmark,
  "subprocess", ...)` in a hermetic unit test, if at all).

## Affected files
- `.redteam/workflows/benchmark.py` — modify: extend task-001's module with
  `extract_metrics`, the default `run_one` (subprocess + tempcopy per the
  isolation strategy above), and `run_benchmark`; keep stdlib-only imports
  and no import-time filesystem access.
- `(new) .redteam/tests/test_benchmark_runner.py` — new hermetic test file
  covering the loop / seam / budget / safety contract listed under
  "To be created" below. Written by the implementer during the `implement`
  phase (agent-pair mode); NOT under `<task_dir>/`.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- tests under `.redteam/tests/` covering **happy path**: 2 configs × 2 tasks ×
  2 repetitions with a stubbed `run_one` returning a deterministic record;
  after `run_benchmark` returns, `results.jsonl` contains exactly 8 records
  in the expected plan order and the stub was called 8 times.
- tests under `.redteam/tests/` covering **resume**: pre-seed 3 of the 8
  triples in `results.jsonl`, then run with the same stub; the stub is
  invoked exactly 5 times and the final JSONL contains all 8 unique
  triples with the pre-seeded 3 preserved verbatim.
- tests under `.redteam/tests/` covering **dry-run**: pre-seed 3 of 8, call
  with `dry_run=True`; the stub is invoked zero times, stdout carries the
  planned + skipped counts and either a numeric cost estimate or the literal
  string `"unknown"` when no prior Claude-cost data exists.
- tests under `.redteam/tests/` covering **budget-abort mid-run**:
  `budget_usd = 1.00`, no pre-seeded records, plan of ≥6 triples, stub
  returns `claude_cost_usd = 0.20` each; the stub is called exactly 5
  times, `run_benchmark` returns exit code `3`, and a clear budget message
  is written to stderr naming the `config`/`task`/`repetition` about to be
  refused.
- tests under `.redteam/tests/` covering **budget scope is per-invocation**:
  the same budget-abort case with `$5.00` worth of historical records
  pre-seeded (their triples excluded from the plan) produces the identical
  5-call count — historical spend does not shrink the in-invocation budget.
- tests under `.redteam/tests/` covering **unknown estimate never aborts
  spuriously**: no pre-seeded records, `budget_usd = 1.00`, stub returns
  `claude_cost_usd = None`; the runner walks the whole plan without a
  false abort.
- tests under `.redteam/tests/` covering **`run_one` raises**: stub raises
  `RuntimeError` on exactly one triple; the loop catches, appends an
  `outcome="error"` record with `config`/`task`/`repetition`/
  `started_at`/`finished_at`, continues to the next triple, and
  `run_benchmark` returns 0.
- tests under `.redteam/tests/` covering **Codex-only cost passthrough**:
  stub returns `claude_cost_usd = None`; the appended JSONL record carries
  `None` verbatim (never coerced to `0.0`).
- tests under `.redteam/tests/` covering **metric extractor**: against a
  synthetic `state` dict with `phase_telemetry` entries mixing claude and
  codex providers plus a `review_code` and a `rescue` entry, plus a
  `retries` dict, the extractor returns the expected `outcome`,
  `review_rounds`, `rescue_count`, `retry_count`, `wall_clock_sec` (sum of
  durations with None → 0.0), and `claude_cost_usd` (sum of Claude-only
  entries; `None` when zero Claude entries).
- tests under `.redteam/tests/` covering **PR-safety static invariants**:
  a grep-style assertion over `.redteam/workflows/benchmark.py` source
  confirms it contains **no** `gh `, no `git push`, no `pr create`, no
  `--force`, and no import of the real `phase_runners.create_pr` module.
- tests under `.redteam/tests/` covering **real config untouched**: a
  hermetic test that stubs `benchmark.subprocess.run` (so no real child is
  spawned) captures the bytes of the real `.redteam/config.toml` before and
  after a `run_one(...)` call and asserts byte-equality; the stubbed
  subprocess call receives an argv whose script path is under the tempcopy
  (not under the real repo) and whose `cwd` is the tempcopy.
- tests under `.redteam/tests/` covering **bootstrap driver correctness**:
  the bootstrap-driver text emitted by `run_one` (either as a module-level
  string constant or a function that returns the text) contains the
  runtime rebind `orchestrator.PHASE_RUNNERS["create_pr"] = ...` and the
  `sys.exit(orchestrator.cmd_start(...))` call, and does NOT contain any
  `gh`, `git push`, or `--force` literal — so the child process cannot
  reach the real `create_pr` runner.

## Risks
- The exact function name of the metric extractor (`extract_metrics` vs
  `derive_metrics` vs a private `_extract`) is unspecified by the brief; the
  implementer picks one — task-003 must import whatever name lands. Same
  freedom applies to the runner's default-`run_one` internal name.
- `retry_count` mapping is pinned to `sum(state.get("retries", {}).values())`
  in this task. If the operator later prefers a per-phase breakdown, that
  is a Phase-2 schema change (would touch `BenchmarkRecord` in task-001's
  module, so it needs its own plan_review).
- `scope_creep_count` reuses `state["deferred_requirements"]` entries. The
  engine writes ceiling-exceeded, stall, and floor-trip deferrals into the
  same list; the extractor filters by a stable `reason`-string predicate
  (the implementer picks the exact prefix by grepping
  `.redteam/workflows/phase_runners/*.py` for `deferred_requirements.append`
  call sites and using an actually-emitted floor-trip `reason` string,
  NOT an invented one). If no existing `reason` cleanly signals floor
  trips, the extractor returns `0` rather than inventing a new event
  field, and the fixture used by the extractor unit test carries that
  same shape.
- The brief permits either extending `benchmark.py` or adding a sibling
  module. This plan pins the single-file option because the affected-files
  budget is a security boundary; a sibling module would require re-opening
  the plan review to widen the `Affected files` list.
- The subprocess isolation strategy assumes `python3` (`sys.executable`),
  `git`, and the harness's runtime dependencies are on PATH inside the
  tempcopy. This is already true for a valid harness install (the harness
  itself is stdlib-only per project rules), and no test spawns a real
  subprocess (the subprocess-shape test stubs `benchmark.subprocess.run`).
  A real `run_one` invocation in production would surface a missing-`git`
  environment as a normal `subprocess.CalledProcessError` that the loop
  converts into an `outcome="error"` record — no repo corruption.
- `create_pr` neutralization relies on `orchestrator.PHASE_RUNNERS` being a
  module-level mutable dict looked up per phase step
  (`orchestrator.py:1364`, verified). If a future engine refactor captures
  `PHASE_RUNNERS[phase]` at module import time or freezes the dict, this
  neutralization silently breaks — the `git remote get-url --push origin`
  preflight in `create_pr.py:82` on a tempcopy with no `origin` is the
  defense-in-depth fallback, but the primary contract needs a follow-up
  regression test in the engine layer (out of scope here — surface to the
  operator as a follow-up ticket).
- The runner is required not to spawn `claude` / `codex` from tests, but
  the default `run_one` in real use DOES spawn a subprocess. Tests must
  exercise ONLY the stubbed `run_one` seam, plus the isolated
  subprocess-stub tests above; if a test accidentally leaves the default
  seam wired without stubbing `benchmark.subprocess.run`, it will spawn a
  real orchestrator subprocess. Enforce hermeticity by never leaving the
  default seam wired without also stubbing `subprocess`.
