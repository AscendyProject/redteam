# task-003 — `benchmark-report` + wire `benchmark`/`benchmark-report` into `orchestrator.main`

## Why this task exists

This is the final layer of the #146 Phase 1 MVP benchmark. It stacks on
`task-002-benchmark-runner` (the loop + `run_one` seam + budget + safety),
which stacks on `task-001-benchmark-config-and-store` (the TOML loader + JSONL
schema/store).

Everything below the operator surface is already there after task-002. What is
still missing:

1. A **`benchmark-report`** implementation that reads `results.jsonl` and
   prints a **markdown table diffing the named configs** on the deterministic
   metrics — **no single hidden score, no Pareto**.
2. Wiring both new subcommands into `orchestrator.main` and the `USAGE`
   string (with `--dry-run` on `benchmark`), so an operator can actually run
   `python3 .redteam/workflows/orchestrator.py benchmark <set>` and
   `... benchmark-report <set>`.

After this task, the whole Phase 1 MVP is operator-usable.

## What to build

### 1. `benchmark-report` — aggregation + markdown diff

Add to `.redteam/workflows/benchmark.py` (or a small sibling module) a
`build_report(config_names: list[str], records: list[dict]) -> str` function
and a `run_report(set_root: Path) -> int` entry point.

- `build_report` takes **the declared config-name list** (from the
  benchmark.toml named configs, in declaration order) **and** the raw records
  list (task-001's `load_records` output), and returns a markdown string. It is
  pure: no I/O. Passing `config_names` in (rather than inferring configs from
  `records` alone) is what lets the report show a **config declared in
  benchmark.toml but with zero records** — resolving decompose PR-001. Row
  order follows `config_names`. `run_report` is the I/O wrapper: it loads the
  benchmark set (task-001's loader) to get `config_names`, reads the JSONL, and
  calls `build_report(config_names, records)`.
- `run_report(set_root)` reads `results.jsonl` via
  `load_records(set_root / "results.jsonl")`, calls `build_report`, prints
  the result to stdout, and returns 0. An empty / missing JSONL prints a
  clear message ("no benchmark results yet — run `orchestrator benchmark
  <set>` first") and returns a non-zero exit code (e.g. `2`).

**Aggregation contract** (deterministic-only, per the parent goal):

Per named config, compute:

- **Sample size**: total records for the config, and per-outcome counts
  (`done` / `deferred` / `error`).
- **Approval rate**: `done_count / total`.
- **Avg review rounds** (across all records — arithmetic mean).
- **Retry rate**: `sum(retry_count) / total`.
- **Rescue rate**: `sum(rescue_count) / total`.
- **Scope-creep rate**: `sum(scope_creep_count) / total`.
- **Avg wall-clock sec**.
- **Claude cost per approved task**: `sum(claude_cost_usd across records
  with outcome == "done" and claude_cost_usd is not None) / done_count`.
  If **no** approved run has a non-None Claude cost, render `n/a`. If
  `done_count == 0`, render `n/a`. Never fabricate.

**Rendering rules**:

- One markdown table. First column = metric name. One column per named
  config, **in the order the configs appear in the JSONL** (which is the
  order task-002 processed them, which is the declaration order from
  `benchmark.toml`).
- Floats formatted to a stable precision (e.g. `f"{x:.2f}"`); rates as
  percentages with two decimals (e.g. `54.17%`); costs as `$0.42` (or
  `n/a`); integers as plain integers.
- **No single "score" column, no Pareto frontier, no "recommended
  winner"** — the goal is a side-by-side diff so the operator reads the
  tradeoffs.
- Below the table, a short "Notes" section listing:
  - the number of records read,
  - which configs (if any) had zero records,
  - which configs (if any) had zero records — derived from `config_names`
    minus the configs present in `records` (this is why `config_names` is a
    parameter),
  - a one-line disclaimer that Codex-role phases contribute `n/a` to the
    Claude cost column (never a fabricated estimate).

### 2. Wire the two subcommands into `orchestrator.main`

Modify `.redteam/workflows/orchestrator.py`:

- Add `benchmark` and `benchmark-report` to the `USAGE` docstring at the
  module top and to the `USAGE` string constant, with a one-line summary
  each and — for `benchmark` — the `--dry-run` flag documented.
- In `main(argv)`, dispatch (the set-root is a **path** to the benchmark set
  directory, mirroring how `start`/`resume` already take a batch-dir path such
  as `.redteam/batches/<batch>` — here `.redteam/benchmarks/<set>`; PR-003):
  - `benchmark <set-root> [--dry-run]` →
    `cmd_benchmark(set_root, dry_run=<flag>)`, which calls
    `benchmark.run_benchmark(set_root, dry_run=..., ...)`.
  - `benchmark-report <set-root>` → `cmd_benchmark_report(set_root)`,
    which calls `benchmark.run_report(set_root)`.
- Argument-parsing discipline matches the existing subcommands
  (`orchestrator status`): reject unknown flags with a clear
  `error: unknown ...` line and return exit code `2`. Reject a missing
  `<set-root>` with `USAGE` and exit `2`. Reject a `<set-root>` that does
  not exist / is not a directory with a clear message and exit `2`.
- Keep both new subcommands strictly separate from the batch pipeline —
  they must NOT trip `_run_pipeline`, must NOT touch batch state, and must
  NOT open a PR. The set root is `.redteam/benchmarks/<name>`, distinct
  from `.redteam/batches/`.

### 3. Nothing else

- Do NOT modify `phase_runners/*`, `config.py`, `adapters/*`, or install
  scripts. If task-002 already exposed the runner via `benchmark.py`,
  this task only adds `run_report` there and a thin `cmd_benchmark` /
  `cmd_benchmark_report` in `orchestrator.py`.

## Tests to add

Add `.redteam/tests/test_benchmark_report_and_cli.py` covering:

- **`build_report` basic diff**: 2 configs, 3 records each with varied
  outcomes → table has exactly 2 config columns in declaration order,
  metric rows for every field listed in the aggregation contract, and
  correct arithmetic (spot-check approval rate + avg wall-clock).
- **Claude cost = `n/a` when every approved run has `claude_cost_usd
  = None`**: verify the rendered cell is literally `n/a`, not `$0.00`.
- **Claude cost = `n/a` when `done_count == 0`**: config with only
  `error`/`deferred` records → cell is `n/a`.
- **`build_report` with a config that has zero records**: call
  `build_report(config_names=["a", "b"], records=<only 'a' has records>)` →
  BOTH `a` and `b` appear as columns in declaration order (`b` shows `n/a` /
  zero-derived cells), and the "Notes" section names `b` as a zero-record
  config. This is exactly the PR-001 case: the declared config list drives the
  columns, so a zero-record config is still shown (it is NOT inferred away).
- **`run_report` on empty JSONL** → non-zero exit + clear message on
  stdout/stderr.
- **`orchestrator.main` CLI dispatch** (via monkeypatched
  `benchmark.run_benchmark` and `benchmark.run_report` — do NOT run
  either for real):
  - `orchestrator benchmark <set>` calls `run_benchmark(set, dry_run=False)`
    and returns its exit code.
  - `orchestrator benchmark --dry-run <set>` **or** `orchestrator benchmark
    <set> --dry-run` calls `run_benchmark(set, dry_run=True)` (pick one
    ordering; document + assert).
  - `orchestrator benchmark-report <set>` calls `run_report(set)`.
  - Unknown flag → exit `2` + `USAGE` on stderr.
  - Missing `<set>` → exit `2` + `USAGE` on stderr.
  - Non-existent `<set>` dir → exit `2` + clear message.
- **`USAGE` string**: both new subcommands appear in the module `USAGE`
  constant (regex/substring assertion). Keeps this file's `USAGE` from
  drifting out of sync with the module docstring.

All CLI tests must monkeypatch `benchmark.run_benchmark` /
`benchmark.run_report` — no real benchmark execution in the test suite.

## Affected files (must stay strictly within these trees)

- `.redteam/workflows/benchmark.py` — **modify** (add `build_report` +
  `run_report`).
- `.redteam/workflows/orchestrator.py` — **modify** (wire `benchmark` +
  `benchmark-report` into `main` and `USAGE`, plus small `cmd_benchmark` /
  `cmd_benchmark_report` helpers).
- `.redteam/tests/test_benchmark_report_and_cli.py` — **new** test file.

Do **not** touch any `phase_runners/*`, `adapters/*`, `config.py`,
`install.py`, the plugin `.claude/commands/*`, or the marketplace/plugin
JSON. Command-surface expansion into the Claude Code plugin is
out-of-scope for the Phase 1 MVP (the CLI alone is the delivery).

## Constraints inherited from the parent

- **Zero runtime dependencies.** Stdlib only.
- **Engine stays project-agnostic.** No stack fingerprints in
  `.redteam/workflows/` or non-example tests; keep
  `test_agents_generic_prompts.py` green.
- **Never auto-merge, never bypass the human checkpoint.** The report
  reads JSONL and prints text — it must not open a PR or call `gh`.
- **Cost honesty.** Codex-only cost cells render `n/a`, never a
  fabricated dollar figure.
- **No single hidden score, no Pareto.** The report shows metrics
  side-by-side. Widening beyond this (Pareto, recommend-models, a
  headline "quality" score, an LLM-judge column) is explicitly a
  non-delegated Phase-2 boundary — do not add it.
- **Do not touch `.redteam/config.toml`** or any batch state.
- **Argument-parsing discipline** matches existing subcommands (`status`
  etc.): unknown-arg / missing-arg / bad-arg all exit `2` with `USAGE`
  or a clear message on stderr.

## Non-goals for this task

- Matrix expansion, Pareto frontier, `recommend-models --profile`,
  LLM-judge scorers, sqlite mirror, external eval-platform export — all
  Phase 2, do NOT build.
- Plugin `.claude/commands/redteam-benchmark.md` — the Claude Code
  plugin surface is deliberately deferred; the CLI is the Phase 1
  operator surface.
- `install.py` changes to seed a starter benchmark set — operators
  create `.redteam/benchmarks/<set>/benchmark.toml` themselves for the
  MVP; a scaffolder is a later convenience.
- Any change to `state.json` / `phase_telemetry` shape or the
  `benchmark.toml` schema — those were fixed in task-001/002.

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

- `build_report` and `run_report` exist in
  `.redteam/workflows/benchmark.py` per the contract above.
- `orchestrator.py`'s `USAGE` string + module docstring list both new
  subcommands, and `main(argv)` dispatches them correctly (with
  `--dry-run` on `benchmark` and clear error handling on bad args).
- `.redteam/tests/test_benchmark_report_and_cli.py` covers every case
  above, with all CLI tests monkeypatching the runner.
- `bash .redteam/scripts/verify.sh` is green.
- No file outside the three affected paths listed above has been
  modified.
