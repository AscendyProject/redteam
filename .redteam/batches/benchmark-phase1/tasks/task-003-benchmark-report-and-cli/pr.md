## What
Ship the last layer of the #146 Phase 1 MVP benchmark: an aggregation +
markdown-diff `benchmark-report` on top of task-002's JSONL, plus CLI wiring
so an operator can run `python3 .redteam/workflows/orchestrator.py benchmark
<set-root> [--dry-run]` and `... benchmark-report <set-root>` end-to-end
against `.redteam/benchmarks/<set>` — no Pareto, no hidden score, no plugin
surface.

## Why
This is the third and final task of the #146 Phase 1 MVP benchmark, stacked
on `task-002-benchmark-runner` (which added the loop + `run_one` seam + budget
+ safety) and `task-001-benchmark-config-and-store` (the TOML loader + JSONL
schema/store). Everything below the operator surface was already in place
after task-002; this task adds the `benchmark-report` aggregation + markdown
diff and wires both new subcommands into `orchestrator.main` so the Phase 1
MVP is operator-usable end-to-end.

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

## Verification
- Tests: test_build_report_basic_table_shape, test_build_report_approval_rate_arithmetic, test_build_report_avg_wall_clock_arithmetic, test_build_report_column_declaration_order, test_build_report_claude_cost_na_when_all_approved_have_none_cost, test_build_report_claude_cost_na_when_done_count_zero, test_build_report_zero_record_config_shown_as_column, test_build_report_zero_record_config_in_notes, test_build_report_zero_record_config_notes_declaration_order, test_build_report_rates_formatted_as_percentage, test_build_report_cost_formatted_as_dollar, test_build_report_notes_has_record_count, test_build_report_notes_codex_disclaimer, test_run_report_missing_jsonl, test_run_report_empty_jsonl, test_main_benchmark_calls_run_benchmark, test_main_benchmark_dry_run_flag, test_main_benchmark_wrong_dry_run_ordering_rejected, test_main_benchmark_report_calls_run_report, test_main_benchmark_returns_runner_exit_code, test_main_benchmark_unknown_flag_rejected, test_main_benchmark_missing_set_root, test_main_benchmark_report_missing_set_root, test_main_benchmark_nonexistent_dir, test_main_benchmark_report_nonexistent_dir, test_main_benchmark_runner_not_called_on_bad_args, test_usage_constant_lists_benchmark_subcommands, test_usage_constant_documents_dry_run
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff scoped to the three approved files only: `.redteam/workflows/benchmark.py`, `.redteam/workflows/orchestrator.py`, and `.redteam/tests/test_benchmark_report_and_cli.py`; no adapter, phase-runner, installer, config, plugin, or marketplace boundary was touched.
- `build_report` keeps declaration-order config columns, keeps zero-record configs, renders `n/a` for zero/unknown cost cases, and avoids Pareto/scores/winner language — produces discriminating output rather than collapsing to constants.
- New tests would have failed pre-change (the functions didn't exist and the orchestrator had no `benchmark`/`benchmark-report` dispatch); all CLI tests monkeypatch the benchmark runners so no real benchmark runs execute in the suite.
- Security checklist: no new non-stdlib runtime imports, no `shell=True`, no verifier allowlist/snapshot changes, no installer changes, no reviewer-adapter capability changes, and no `gh`/PR creation path added.
- `state.json` records `verification.last_exit_code == 0` and `verification.log` shows `847 passed` with `verify.sh OK`.
- REVIEW_DECISION: APPROVED (no open IR findings).

## Generated by
redteam / batch benchmark-phase1 / task task-003-benchmark-report-and-cli
