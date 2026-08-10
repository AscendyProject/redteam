# Outcome — `benchmark-report` + wire `benchmark` / `benchmark-report` into `orchestrator.main`

## Goal
Ship the last layer of the #146 Phase 1 MVP benchmark: an aggregation +
markdown-diff `benchmark-report` on top of task-002's JSONL, plus CLI wiring
so an operator can run `python3 .redteam/workflows/orchestrator.py benchmark
<set-root> [--dry-run]` and `... benchmark-report <set-root>` end-to-end
against `.redteam/benchmarks/<set>` — no Pareto, no hidden score, no plugin
surface.

## Done-when
- [ ] `.redteam/workflows/benchmark.py` defines a pure `build_report(config_names: list[str], records: list[dict]) -> str` that returns a markdown table with one column per name in `config_names` (declaration order, kept even when a name has zero records), one row per aggregated metric (sample size + per-outcome counts, approval rate, avg review rounds, retry rate, rescue rate, scope-creep rate, avg wall-clock sec, Claude cost per approved task), followed by a "Notes" section listing the record count, any zero-record configs (derived from `config_names` minus configs present in `records`), and the Codex-cost `n/a` disclaimer.
- [ ] Formatting in `build_report` output: floats `f"{x:.2f}"`, rates as `%` with two decimals (e.g. `54.17%`), costs as `$0.42` / `n/a`, integers rendered plain — asserted by tests.
- [ ] Claude-cost cell renders `n/a` (never `$0.00`) whenever `done_count == 0` OR every done-outcome record has `claude_cost_usd is None` — asserted by tests.
- [ ] `.redteam/workflows/benchmark.py` defines `run_report(set_root: Path) -> int` that reads `set_root / "results.jsonl"` via `load_records`, obtains `config_names` from `load_benchmark_set(set_root).configs` in declaration order, prints `build_report(config_names, records)` to stdout and returns `0`; when the JSONL is missing OR contains zero records it prints a clear message (naming `orchestrator benchmark <set>` as the next step) and returns a non-zero exit code (`2`).
- [ ] `.redteam/workflows/orchestrator.py`'s module docstring **and** `USAGE` string constant both list `benchmark <set-root> [--dry-run]` and `benchmark-report <set-root>` with one-line summaries — asserted by a substring test over the `USAGE` constant.
- [ ] `main(argv)` in `orchestrator.py` dispatches `benchmark <set-root> [--dry-run]` to a `cmd_benchmark(set_root, dry_run=...)` helper that calls `benchmark.run_benchmark(set_root, dry_run=...)`, and `benchmark-report <set-root>` to a `cmd_benchmark_report(set_root)` helper that calls `benchmark.run_report(set_root)`; both helpers return the callee's exit code.
- [ ] `main` accepts exactly one `--dry-run` position for `benchmark` (either `benchmark --dry-run <set>` or `benchmark <set> --dry-run` — pick one, document in `USAGE`, assert in tests); the un-picked ordering plus any other flag returns `2` with `USAGE` on stderr and does NOT call `run_benchmark`.
- [ ] `main` rejects: (a) a missing `<set-root>` for either subcommand → exit `2` + `USAGE` on stderr, (b) a `<set-root>` path that does not exist or is not a directory → exit `2` + clear message on stderr, (c) any unknown flag on `benchmark` / `benchmark-report` → exit `2` + `USAGE`-or-clear-message on stderr. In none of these cases is `run_benchmark` / `run_report` invoked (verified by monkeypatch-fail-if-called).
- [ ] Neither new subcommand touches `_run_pipeline`, batch state, `create_pr`, or `gh` — enforced by keeping the dispatch in the `main` `if command == ...` ladder above the batch commands and by tests that monkeypatch `benchmark.run_benchmark` / `benchmark.run_report` and observe only those calls fire.
- [ ] A new `.redteam/tests/test_benchmark_report_and_cli.py` covers every case listed in input.md's "Tests to add" section — pure `build_report` diff arithmetic (approval rate + avg wall-clock spot-check), the two `n/a` Claude-cost paths, the zero-record-config PR-001 case, `run_report` on empty/missing JSONL, all `orchestrator.main` dispatch and error-handling cases with `benchmark.run_benchmark` / `benchmark.run_report` monkeypatched (no real benchmark execution), and the `USAGE` substring assertion.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff + full pytest, including `test_agents_generic_prompts.py`).
- [ ] Only the three files listed under "Affected files" have been modified/created; nothing under `phase_runners/`, `adapters/`, `config.py`, `install.py`, `.claude/`, or the plugin/marketplace JSON has changed (checked via `git diff --name-only` in review).

## Out of scope
- Matrix expansion, Pareto frontier, `recommend-models --profile`, LLM-judge scorers, sqlite mirror, external eval-platform export (all Phase 2).
- A Claude Code plugin command (`.claude/commands/redteam-benchmark.md` / marketplace/plugin JSON): the CLI is Phase 1's operator surface.
- `install.py` changes to seed a starter benchmark set.
- Any change to `state.json` / `phase_telemetry` shape, the `benchmark.toml` schema, the `results.jsonl` schema, or the `extract_metrics` / `run_benchmark` / `run_one` behavior fixed by task-001/002.
- Modifying `.redteam/config.toml` or any batch state.
- Adding a headline "quality" score column or any single-number ranking.

## Affected files
- `.redteam/workflows/benchmark.py` — add `build_report` (pure) + `run_report` (I/O wrapper).
- `.redteam/workflows/orchestrator.py` — extend `USAGE` docstring + `USAGE` constant; add `cmd_benchmark` / `cmd_benchmark_report`; wire both into `main(argv)` with `--dry-run` handling and the standard unknown/missing/bad-arg → exit-2 discipline.
- `(new) .redteam/tests/test_benchmark_report_and_cli.py` — covers `build_report`, `run_report`, CLI dispatch (monkeypatched), and the `USAGE` substring assertion; created here at the canonical test location by the pipeline's implement phase, NOT under the task dir.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- tests under `.redteam/tests/` covering: `build_report` markdown table shape + arithmetic (approval rate, avg wall-clock, retry / rescue / scope-creep rate), declaration-order column ordering, zero-record-config PR-001 case (column kept + Notes lists it), both `n/a` Claude-cost paths (`done_count == 0`, all `claude_cost_usd is None`), formatting rules (`.2f` floats, `%` rates, `$` costs, plain ints), `run_report` on empty/missing JSONL (non-zero exit + operator-facing message), `orchestrator.main` dispatch for `benchmark` (both with and without `--dry-run` in the chosen ordering), `benchmark-report`, unknown-flag / missing-arg / non-existent-dir all → exit `2` with `USAGE` or clear message, `USAGE` constant substring check for both new subcommands, and a "runner not called on bad args" assertion via monkeypatched `benchmark.run_benchmark` / `benchmark.run_report`.
- All CLI tests must monkeypatch `benchmark.run_benchmark` and `benchmark.run_report` — no real benchmark execution in the test suite.

## Risks
- **`--dry-run` flag position.** Input.md leaves the ordering `benchmark --dry-run <set>` vs `benchmark <set> --dry-run` to the implementer ("pick one, document + assert"). The implementer must commit to one ordering, document it in `USAGE`, and assert both the accepted ordering AND the rejected one (exit `2`). Cross-check against `cmd_status`'s handling (`argv[3:] == ["--json"]`) for consistency, but they are independent — this is not a project-wide flag-parsing refactor.
- **`run_report` exit code on missing JSONL.** Input.md says "e.g. `2`". `2` is already this project's "bad args / usage" code; some readers may prefer a distinct code (e.g. `4`) to distinguish "no data yet" from "unusable invocation". Recommend sticking with `2` for consistency with the surrounding CLI discipline — flag if the reviewer prefers a distinct code.
- **`USAGE` module docstring vs `USAGE` constant.** The current file carries both (lines 4–12 docstring + lines 2521–2542 constant). Keeping them in sync is a manual chore; the substring test only guards the constant. Adding a docstring-substring test is arguably better hygiene but is outside the Done-when — flag if the reviewer wants both guarded.
- **Codex-role cost disclaimer wording.** The Notes line about `n/a` Codex cost is prose. If the operator later wants to grep this line, the exact wording matters. The tests should pin at least a stable substring (e.g. `"Codex"` and `"n/a"`).
- **Zero-record-config detection via set difference.** `set(config_names) - {r["config"] for r in records}` loses declaration order for the Notes list. The implementer must iterate `config_names` and filter — an ordered-list comprehension, not a set diff — so the Notes line is deterministic; call out in the test.
