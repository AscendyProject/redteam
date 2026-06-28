# Outcome — Make `run_claude` per-phase timeout a true wall-clock bound (#109)

## Goal
`run_claude` in `.redteam/workflows/phase_runners/_base.py` enforces its
`timeout_sec` as a HARD wall-clock deadline that fires even when the worker
subprocess is alive but silent (no stdout writes). On such a hang it returns
the same fail-closed `ClaudeRunResult(returncode=124, …)` it returns today on
the stream-driven timeout — so the orchestrator can never be wedged
indefinitely by an upstream API/network stall.

## Done-when
- [ ] `run_claude` in `.redteam/workflows/phase_runners/_base.py` arms a
  wall-clock deadline (the brief's intended approach is `threading.Timer`)
  that kills `proc` at `timeout_sec` independent of any read on `proc.stdout`
  — i.e. the deadline does NOT depend on a new stdout line arriving.
- [ ] When the deadline fires, `run_claude` returns
  `ClaudeRunResult(returncode=124, stdout="".join(raw_lines),
  stderr=f"timeout after {timeout_sec}s\n{stderr_tail[:2000]}",
  parsed_json=final_result)` — the existing fail-closed shape, NOT
  `proc.returncode` (which would be the SIGKILL `-9`).
- [ ] The deadline-fired vs normal-exit distinction is made by an in-process
  signal set by the kill callback (e.g. a `threading.Event`), not by reading
  `proc.returncode` after the loop unwinds.
- [ ] The wall-clock timer is cancelled on every exit path of `run_claude`
  (normal completion, deadline-kill return, and the existing `Exception →
  returncode=125` branch) — verified by a `finally` (or equivalent) so no
  `threading.Timer` thread leaks per call.
- [ ] The post-EOF `proc.wait()` on the normal path is bounded (e.g.
  `proc.wait(timeout=5)`) so a child that closes stdout but does not exit
  cannot hang there either.
- [ ] All existing fail-closed branches are preserved verbatim: the
  `FileNotFoundError → returncode=127` early-return, the `Exception →
  returncode=125` stream-read branch, `encoding="utf-8"`, `bufsize=1`,
  `subprocess.PIPE` for stdout/stderr, and the live `_print_stream_event`
  printing + `type:"result" → parsed_json` capture on the non-timeout path.
- [ ] No new non-stdlib import is added to `_base.py` (zero runtime deps);
  only `threading` / `subprocess` / `time` / existing imports are used.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff format
  --check + full pytest), including the unchanged
  `.redteam/tests/test_run_claude_model.py` suite that monkeypatches
  `base.subprocess.Popen` with the `_FakeProc` style.
- [ ] A new deterministic, fast regression test under `.redteam/tests/`
  (file matching `test_*.py`) proves the hard bound: with a fake
  `subprocess.Popen` whose `.stdout` yields one line and then blocks well
  past a SMALL injected `timeout_sec`, `run_claude` returns
  `returncode == 124` in wall-clock time well under that fake's block
  duration (and well under the 900s default). The test does not actually
  sleep for `DEFAULT_TIMEOUT_SEC` and does not depend on a wall-clock
  margin that would flake on a slow CI box.
- [ ] A second new test asserts the normal path is unchanged: a fake proc
  that emits one `type:"result"` event and exits returns `returncode == 0`
  with `parsed_json` populated and no lingering active `threading.Timer`
  (timer was cancelled).

## Out of scope
- `.redteam/workflows/adapters/codex.py` — already enforces a true
  wall-clock bound via `subprocess.run(timeout=)` + `TimeoutExpired`; do
  not touch it. This task only closes the asymmetry on the worker path.
- A "stall watchdog" variant (per-line timer reset; kill after N seconds
  of *no* output; surfacing "worker silent for Ns" into `progress.md`) —
  out of scope; implement the simplest hard wall-clock deadline that
  matches the reviewer adapter's semantics.
- Changing the `DEFAULT_TIMEOUT_SEC = 900` value or `max_retries_per_phase`.
- Changing the caller contract: callers branch on `returncode == 124`;
  that mapping stays as-is, the stdout/stderr/parsed_json fields stay
  as-is.
- Any change to other helpers in `_base.py` (`compute_repo_diff`,
  `commit_paths`, `pinned_base_branch`, etc.).
- #112 / untracked-baseline behavior — unrelated, already merged.

## Affected files
- `.redteam/workflows/phase_runners/_base.py` — modify the `run_claude`
  read-loop / deadline / post-EOF `proc.wait()` region to install a
  wall-clock `threading.Timer` (or equivalent stdlib-only mechanism),
  cancel it in `finally`, and bound the post-EOF wait. Locate by symbol.
- `(new) .redteam/tests/test_run_claude_timeout.py` — regression tests for
  the silent-hang → 124 bound, the normal path returning 0 with the timer
  cancelled, and the no-thread-leak invariant on the timeout path. (Test
  file is created by the pipeline's test-writing phase at this canonical
  location, NOT under `<task_dir>/`.)

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  --check + pytest over `.redteam/`) must pass.
- `.redteam/tests/test_run_claude_model.py` — the existing `_FakeProc`
  monkeypatch suite (`test_run_claude_passes_model_when_configured`,
  `test_run_claude_pins_utf8_encoding`, `test_run_claude_omits_model_when_none`,
  `test_run_claude_honors_permission_mode_env_override`,
  `test_run_claude_rejects_unknown_permission_mode`,
  `test_run_claude_passes_allowed_tools_when_set`) must still pass with
  the unchanged `_FakeProc` shape (iterable `.stdout`, `.stderr`,
  `.returncode`, `.wait(timeout=…)`, `.kill()`).

### To be created (the test-writing phase will define exact test names)
Tests under `.redteam/tests/` (file matching `test_*.py`, e.g.
`test_run_claude_timeout.py`) covering:
- Silent-hang hard bound: with a fake `subprocess.Popen` whose `.stdout`
  yields one valid stream-json line and then blocks (e.g. on a
  `threading.Event.wait()` or similar) far past a tiny injected
  `timeout_sec`, `run_claude` returns `returncode == 124` in wall-clock
  time well under the fake's block duration, with the existing
  stdout/stderr/`parsed_json` shape, and `proc.kill()` was called.
- Timer-kill returncode contract: the result is `124`, NOT `proc.returncode`
  (which would be the SIGKILL value), so callers' `== 124` fail-closed
  branch is preserved.
- Normal-path unchanged: a fake proc that emits one `type:"result"` event
  and exits returns `returncode == 0`, populates `parsed_json` from the
  result event, and leaves no active `threading.Timer` (cancelled in
  `finally`).
- Bounded post-EOF wait: a fake proc whose stdout closes but whose
  `wait()` would otherwise block does not hang `run_claude` indefinitely
  on the normal path.
- Exception path still cancels the timer: when the stream read raises,
  `run_claude` returns `returncode == 125` and no timer is left active.

## Risks
- The brief calls out `threading.Timer` as the intended approach but
  notes "confirm in plan_review" — the planning phase may prefer a
  different stdlib-only mechanism (e.g. a watchdog thread with an
  `Event`, or `signal.alarm` — though `signal.alarm` is POSIX-only and
  this repo supports cp949/Windows hosts per #48, so `threading.Timer`
  is the cross-platform choice). Decision belongs to plan_review.
- This change touches the fail-closed timeout guarantee — a security
  boundary per `CLAUDE.md`. Per repo policy ("security-boundary or
  multi-file changes go through Codex review"), the implementer's diff
  must clear adversarial review before merge; this outcome assumes the
  pipeline's reviewer phase is the gate.
- Writing a deterministic test for a wall-clock timeout is inherently
  timing-adjacent. The test must inject a small `timeout_sec` and a
  fake whose "block" is long enough to make a false-positive impossible
  but short enough not to slow the suite — exact margins are a
  test-writing-phase call, not an outcome decision.
- The fix is dogfooded through the same worker path it changes; a
  mid-flight interrupt on the new test file could intersect the
  (separately fixed) #112 hazard. Operational note only — not a scope
  change.
