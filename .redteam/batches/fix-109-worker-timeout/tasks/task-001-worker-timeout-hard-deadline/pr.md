## What
`run_claude` in `.redteam/workflows/phase_runners/_base.py` enforces its
`timeout_sec` as a HARD wall-clock deadline that fires even when the worker
subprocess is alive but silent (no stdout writes). On such a hang it returns
the same fail-closed `ClaudeRunResult(returncode=124, …)` it returns today on
the stream-driven timeout — so the orchestrator can never be wedged
indefinitely by an upstream API/network stall.

## Why
Issue #109: today's deadline check is stream-driven (`time.monotonic() > deadline`
is only evaluated *after* a new stdout line arrives), so the 900s bound is soft —
a worker that goes silent (upstream API/network stall) parks `for line in
proc.stdout` in a blocking read indefinitely. The post-EOF `proc.wait()` (no
timeout) is the same bug at lower risk. The reviewer/Codex adapter already
enforces a true wall-clock bound via `subprocess.run(timeout=)`; this closes the
asymmetry on the worker path so an orchestrator run can no longer be wedged
forever by a silent worker subprocess.

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

## Verification
- Tests: `test_silent_hang_returns_124`, `test_timer_kill_returns_124_not_sigkill`, `test_normal_path_returncode_and_parsed_json`, `test_normal_path_timer_is_cancelled`, `test_normal_path_wait_is_bounded`, `test_post_eof_wait_timeout_returns_124`, `test_timer_fired_during_post_eof_wait_returns_124`, `test_exception_path_returns_125_and_cancels_timer`
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff summary: `_base.py` `run_claude` now arms a `threading.Timer(timeout_sec, _kill_on_timeout)` started before the stdout loop and cancelled in `finally`; the kill callback sets a `threading.Event` so a timer-fired kill is distinguishable from a normal exit and always returned as `124` (never the SIGKILL `-9`). Post-EOF `proc.wait()` is bounded via `wait(timeout=5)`; `TimeoutExpired` and a post-wait `timed_out` re-check both map to the same fail-closed `124` shape.
- All existing fail-closed branches (FileNotFoundError → 127, Exception → 125, encoding="utf-8", bufsize=1, PIPE, stream-event printing + `type:"result"` capture) are preserved verbatim; only stdlib (`threading`/`subprocess`/`time`) is imported.
- IR-001 (round 1, major): post-EOF `proc.wait(timeout=5)` raising `TimeoutExpired` escaping `run_claude` — RESOLVED by catching it and returning `124` fail-closed.
- IR-002 (round 2, major): timer-during-`proc.wait()` race leaking `proc.returncode=-9` instead of `124` — RESOLVED by re-checking `timed_out.is_set()` after `proc.wait()` returns.
- Reviewer (Codex) final decision: APPROVED. New tests are discriminating against `main` (silent-hang and SIGKILL-mapping tests would hang/fail there); `bash .redteam/scripts/verify.sh` passed (ruff + 476 pytest tests, `state.json.verification.last_exit_code == 0`).
- No HITs (no human-intervention-required items); no `Disagree` / `Uncertain` findings from the reviewer.

## Generated by
redteam / batch fix-109-worker-timeout / task task-001-worker-timeout-hard-deadline
