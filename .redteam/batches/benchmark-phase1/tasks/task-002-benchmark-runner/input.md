# task-002 — `benchmark` runner (loop + `run_one` seam + budget + safety)

## Why this task exists

This is the **execution layer** of the #146 Phase 1 MVP benchmark. It stacks
directly on `task-001-benchmark-config-and-store` (which shipped the
`benchmark.toml` loader + JSONL append/resume helpers). This task wires the
outer loop:

```
for config in benchmark_set.configs:
    for task in benchmark_set.tasks:
        for rep in 1..repetitions:
            if (config, task, rep) already in results.jsonl: skip
            if would exceed budget_usd: abort BEFORE dispatch
            record = run_one(set, config, task, rep)   # <-- injectable seam
            append_record(results.jsonl, record)
```

Everything about *actually* running a pipeline lives behind a single injectable
seam (`run_one`) so the whole loop is testable **without a real model, without
subprocess, without a real pipeline**. That testability is a hard constraint
of the parent goal — treat it as a security/safety boundary, not a nicety.

The runner must **never corrupt the operator's real repo**: no mutation of
`.redteam/config.toml`, no leftover branches, no dirty working tree. The MVP
may take the sequential temp-config approach (write a scratch config in a
temp dir, point the child pipeline at it, restore/discard on exit) — full
worktree isolation is fine but not required for the MVP.

Task-003 will wire this runner into `orchestrator.main` + a
`benchmark-report` subcommand; **this task does not touch `orchestrator.py`**
except (if needed) to add an internal helper that `orchestrator.py` will
later import — CLI/USAGE plumbing is deferred to task-003.

## What to build

Extend `.redteam/workflows/benchmark.py` (or add a sibling module — pick
whichever keeps the file readable) with:

1. **A `run_one` seam**:
   - Signature suggestion:
     `run_one(set_root: Path, config_name: str, task_id: str, repetition: int,
     *, config_overrides: Mapping[str, str], workspace: Path) -> dict`
     returning a result record that matches the task-001 JSONL schema.
   - The **default implementation** is the real one: it (a) writes the merged
     `.redteam/config.toml` (base config + this run's role overrides) to a
     temp/isolated location, (b) copies the task's `input.md` into a scratch
     batch dir under a **temp** path (e.g. `<workspace>/batches/<config>-<task>-<rep>/tasks/<task>/input.md`),
     (c) drives the pipeline via existing engine entry points (`_run_pipeline`
     / `process_task`), (d) reads `state.json` + `state["phase_telemetry"]`
     from the completed task dir, (e) derives the deterministic metrics
     (see below) and returns them as a record dict.
   - **Contract for tests**: `run_one` MUST be replaceable by a stub via
     dependency injection. The concrete seam design is up to the implementer,
     but a plain module-level function that the runner calls through a
     `run_one` **parameter** (default = the real impl) — e.g.
     `run_benchmark(set_root, ..., run_one=_default_run_one)` — is the
     simplest form and what tests will exercise. Do NOT rely on
     monkeypatching `subprocess` or `_run_pipeline` from the outside; put the
     seam in the runner's own signature.
   - **Metric extraction** from a completed task's `state.json`:
     - `outcome`: `"done"` if `state["next_phase"] == "done"`; `"deferred"`
       if `state.get("deferred") == True` or `next_phase == "deferred"`;
       otherwise `"error"`.
     - `review_rounds`: count of `phase == "review_code"` entries in
       `state["phase_telemetry"]` (or `state["review_rounds"]` if the engine
       already tracks it — read whichever the engine actually stores; prefer
       telemetry so no schema change is needed).
     - `retry_count`: count of retries recorded in `state` (use existing
       fields; do NOT add new state.json keys in this task).
     - `rescue_count`: count of `phase == "rescue"` entries in
       `state["phase_telemetry"]` (or the engine's existing counter).
     - `scope_creep_count`: count of floor-trip events already surfaced in
       `state` (e.g. `deferral` entries whose cause is a floor trip — reuse
       what the engine emits; do NOT invent a new event).
     - `wall_clock_sec`: sum of `duration_sec` across `phase_telemetry`
       (float; None entries contribute 0.0).
     - `claude_cost_usd`: sum of `cost_usd` across `phase_telemetry` entries
       whose `provider == "claude"`; if **every** entry is Codex (or no
       Claude entry exists), return `None`. **Never fabricate** a cost from
       a Codex-only run — `None` → the report will render `n/a`.
   - Wall-clock timing is measured by `run_one` itself (from just before
     dispatch to just after the pipeline returns), NOT re-derived only from
     telemetry sums — telemetry misses phases like plan_review that invoke
     no worker. Use `time.monotonic()`.

2. **`run_benchmark(set_root, *, dry_run=False, run_one=_default_run_one) -> int`**
   — the outer loop:
   - Load the set via task-001's loader.
   - Load existing records via `load_records`; compute `completed_triples`.
   - Compute the **planned run list**: `[(config, task, rep) for config in
     set.configs for task in set.tasks for rep in 1..set.repetitions
     if (config, task, rep) not in completed]`.
   - If `dry_run`: print (a) planned run count, (b) skipped-because-already-done
     count, (c) rough cost estimate = `mean(claude_cost_usd of prior records
     where not None) * planned_count`, or the literal string `"unknown"` if
     no prior Claude cost data exists; then return **0 without running
     anything**.
   - Otherwise, iterate the plan in order:
     - **Budget check BEFORE dispatch**: compute
       `accumulated = sum of claude_cost_usd of records appended THIS invocation
       (Nones contribute 0)`. If `set.budget_usd is not None` AND
       `accumulated + estimated_next >= set.budget_usd`, abort **before**
       calling `run_one` with a clear stderr message
       (`benchmark: aborting before <config>/<task>/rep=<n>: accumulated
       $X.XX + estimate $Y.YY would exceed budget $Z.ZZ`) and return a
       non-zero exit code (e.g. `3`) to signal fail-closed cost stop.
       `estimated_next` = same mean-of-prior-Claude-costs used by dry-run
       (or `0.0` if unknown — the goal says "would exceed", so an unknown
       estimate does NOT trigger an abort by itself).
     - Call `run_one(...)`; on exception, catch, build an `outcome="error"`
       record with as many fields populated as possible (at minimum
       `config`/`task`/`repetition`/`started_at`/`finished_at`/`outcome`),
       append it, and **continue** to the next triple (one bad run must not
       tank the whole benchmark).
     - `append_record(...)` immediately after every run (so a `Ctrl-C`
       between runs still resumes cleanly next invocation).
   - Return 0 on normal completion.

3. **Repo-safety scaffolding** — this is the security boundary:
   - Materialize the merged per-run config in a temp path
     (`tempfile.TemporaryDirectory()` under a workspace root, or a subdir of
     the set root under `.gitignore`d `results/` if worktree isolation is
     preferred). **Do NOT write over the real `.redteam/config.toml`.**
   - The child pipeline receives its config via an explicit path (existing
     engine seam: `load_config` already accepts a repo root — thread the temp
     root through; do NOT patch env vars globally).
   - After each run, ensure the operator's working tree is clean of
     benchmark scratch (delete the temp dir; leave `results.jsonl` and its
     parent alone).
   - **Never open PRs, never merge, never touch remotes.** The runner must
     not call `create_pr` or `gh` in any code path.

## Tests to add

Add `.redteam/tests/test_benchmark_runner.py` covering (all with a stubbed
`run_one` — no subprocess, no adapter calls, no real pipeline):

- **Happy path**: 2 configs × 2 tasks × 2 reps; a stub `run_one` returns a
  deterministic record; after `run_benchmark` returns, `results.jsonl` has
  exactly 8 records in the expected order.
- **Resume**: pre-seed `results.jsonl` with 3 of the 8 triples; run again;
  stub `run_one` is invoked exactly 5 times.
- **Dry-run**: pre-seed 3 of 8; call with `dry_run=True`; stub `run_one` is
  invoked **zero** times; stdout carries the planned/skipped counts + a cost
  estimate (or the string `"unknown"` when no prior Claude cost).
- **Budget is PER-INVOCATION (decompose PR-002).** The cap bounds only the
  Claude cost accumulated by runs dispatched in THIS `run_benchmark` call —
  prior JSONL records (already spent, and skipped by resume) do NOT count toward
  it. (Cumulative-across-resumes budgeting is a Phase-2 refinement; keep it
  per-invocation for the MVP, matching the parent goal's "this invocation"
  wording.) Document this in the code and assert it in the test below.
- **Budget abort mid-run**: set `budget_usd = 1.00`, `repetitions` and configs
  so >5 triples are pending, and **no pre-seeded records** (so historical cost
  is irrelevant — proving per-invocation semantics); stub `run_one` returns a
  record worth `$0.20` each. Run: the runner dispatches until the in-invocation
  accumulated cost + the next estimate would reach/exceed `$1.00`, then aborts
  **before** that dispatch — so the stub is called exactly `5` times (5 × $0.20
  = $1.00, and the 6th is refused), the call returns non-zero, and a clear
  budget message is printed on stderr. Add a second assertion that pre-seeding
  historical records worth `$5.00` does NOT change this count (confirming
  history is excluded).
- **Budget with unknown estimate does NOT trigger abort**: no prior records,
  `budget_usd = 1.00`, stub returns `claude_cost_usd = None` — the runner
  proceeds through the plan without a false abort.
- **`run_one` raises**: stub raises `RuntimeError` on one triple; runner
  catches, appends an `outcome="error"` record, continues, and returns 0.
- **Codex-only cost**: stub returns `claude_cost_usd = None`; the record is
  appended verbatim (None, not 0.0).
- **Metric-extraction unit** (against a fixture `state.json` dict): given a
  synthetic `state` with `phase_telemetry` covering claude + codex entries,
  the extractor returns the expected `claude_cost_usd` (Claude entries
  summed; None if no Claude entries), `wall_clock_sec`, and phase counts.

Keep the tests hermetic: `tmp_path` for the JSONL, in-memory dicts for
`state.json`, stub `run_one`. Do not import `adapters/*` or spawn any
subprocess in this test file.

## Affected files (must stay strictly within these trees)

- `.redteam/workflows/benchmark.py` — **modify** (extend task-001's module
  with the runner + `run_one` seam + metric extractor).
- `.redteam/tests/test_benchmark_runner.py` — **new** test file.

Do **not** modify `orchestrator.py`, `config.py`, any `phase_runners/*`, or
any adapter in this task — CLI wiring is task-003 and pipeline internals
are out of scope. If a small helper truly must land in `orchestrator.py`
(e.g. to expose `_run_pipeline` for `run_one` to call), keep it to a
one-line reference; do not refactor the module.

## Constraints inherited from the parent

- **Zero runtime dependencies.** Stdlib only (`tomllib`, `json`,
  `statistics`, `subprocess`, `pathlib`, `tempfile`, `time`, `datetime`).
- **Engine stays project-agnostic.** No stack fingerprints in
  `.redteam/workflows/` or non-example tests; keep
  `test_agents_generic_prompts.py` green.
- **Testability without real model runs is a hard constraint.** The whole
  outer loop MUST be exercisable via a stub `run_one`. No test may spawn
  `claude` or `codex`.
- **Must never corrupt the real repo.** No writes to
  `.redteam/config.toml`, no leftover branches, no dirty worktree. The
  merged per-run config lives in a temp dir; scratch is deleted after each
  run. This is a safety boundary — **do not hand-wave it**.
- **Never auto-merge, never bypass the human checkpoint.** The runner
  measures only; no PR opening, no `gh` calls, no merges.
- **Cost telemetry honesty.** Codex-only runs → `claude_cost_usd = None`,
  reported as `n/a` downstream. Never fabricate a cost.
- **Fail-loud on cost.** Budget check runs **before** dispatch. Aborting
  mid-benchmark on budget is an exit-nonzero, clear-message event, not a
  silent skip.

## Non-goals for this task

- Any change to `orchestrator.main` / `USAGE` (task-003).
- The markdown-diff report and its aggregation (task-003).
- Matrix expansion, Pareto frontier, `recommend-models`, LLM-judge
  scorers, sqlite mirror — Phase 2, do NOT build.
- Changing `state.json` / `phase_telemetry` shape — read what task-001
  (#150) already emits; do not add new keys.
- Historical-batch conversion (`.redteam/batches/*` → benchmark set).

## Verification

The task's `outcome.md` must carry a parseable `## Verification` section
whose fenced ```yaml `commands:` list is exactly:

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

**Do NOT add a bare `pytest …` line** — bare `pytest` is not on PATH in this
venv-based repo and the verification exec would fail. `verify.sh` already
runs `ruff` + `pytest` over `.redteam/`.

## Done when

- `run_benchmark` + a default `run_one` + metric extractor exist inside
  `.redteam/workflows/benchmark.py` and behave per the contract above.
- `.redteam/tests/test_benchmark_runner.py` exists and covers every test
  case listed above, all hermetic.
- `bash .redteam/scripts/verify.sh` is green.
- No file outside the two affected trees has been modified.
- Manual read-through confirms: no code path writes to
  `.redteam/config.toml`, no path opens a PR / calls `gh` / touches
  remotes.
