# Make the worker per-phase timeout a true wall-clock bound (#109)

## Goal
`run_claude` in `.redteam/workflows/phase_runners/_base.py` must enforce its
`timeout_sec` (default `DEFAULT_TIMEOUT_SEC = 900`) as a HARD wall-clock deadline
that fires even when the worker subprocess goes completely silent (alive but
emitting no stdout — e.g. an upstream API/network hang). After the fix, a worker
that connects, prints one stream-json event, then blocks forever without writing
another byte is killed at `timeout_sec` and `run_claude` returns the SAME
fail-closed result it returns today on the stream-driven timeout
(`returncode=124`), instead of blocking the orchestrator indefinitely.

## What to build
Today the deadline is stream-driven, not wall-clock. In `run_claude`:
```python
deadline = time.monotonic() + timeout_sec
for line in proc.stdout:                 # blocks in a read while the child is silent
    if time.monotonic() > deadline:      # only evaluated AFTER a line arrives
        proc.kill(); proc.wait(timeout=5)
        return ClaudeRunResult(returncode=124, ...)
    ...
proc.wait()                              # post-EOF wait is ALSO unbounded
```
The `time.monotonic() > deadline` check sits inside the iterator body, so it is
reached only when a new line is read. If the child produces no output,
`for line in proc.stdout` parks in a blocking read and the deadline is never
compared. The 900s bound is therefore *soft* — it depends on the worker
continuing to emit output, exactly the condition that fails during a silent hang.
The post-EOF `proc.wait()` (no timeout) is the same class of bug at lower risk.

Make the deadline independent of stream liveness. The intended approach (confirm
in plan_review) is a `threading.Timer(timeout_sec, ...)` that kills the process at
the deadline regardless of output:
- Start the timer before the read loop; `cancel()` it in a `finally`.
- The timer callback must both `proc.kill()` AND record that the kill was a
  timeout (e.g. set a `threading.Event`), so that after the loop unwinds (the pipe
  closes on kill and `for line in proc.stdout` ends) the function can distinguish
  a timeout-kill from a normal exit and return `returncode=124` with the existing
  `f"timeout after {timeout_sec}s\n{stderr_tail[:2000]}"` stderr — NOT the raw
  `proc.returncode` (which would be the SIGKILL `-9`). The `returncode=124`
  fail-closed contract callers rely on must be preserved exactly.
- Bound the post-EOF `proc.wait()` (e.g. `proc.wait(timeout=5)`) so a child that
  closes stdout but does not exit cannot hang there either.

The existing in-loop deadline check may be kept or removed — but correctness must
NOT depend on it (the timer is now the real bound). Keep the live one-line stream
printing (`_print_stream_event`) and the `type:"result"` capture into
`parsed_json` working unchanged for the normal (non-timeout) path.

## Constraints
- **Robustness boundary — touches the fail-closed timeout guarantee. plan_review
  FIRST before any code.**
- Engine stays project-agnostic and **stdlib-only (zero runtime deps)** —
  `threading` / `subprocess` / `time` are stdlib; do not add a dependency.
- Preserve the existing fail-closed semantics: a timeout returns
  `ClaudeRunResult(returncode=124, ...)` with the captured partial stdout
  (`"".join(raw_lines)`) and `parsed_json=final_result` as today. Callers branch on
  `returncode == 124`; do not change that contract.
- Preserve `encoding="utf-8"` (#32 / cp949), `bufsize=1`, the `FileNotFoundError`
  → `returncode=127` path, and the `Exception` → `returncode=125` stream-read path.
- No thread leak: the `Timer` must be cancelled on every exit path (normal,
  timeout, and the `except` path), e.g. via `finally`.
- Do not introduce a busy-wait / polling loop; the timer must not depend on the
  worker emitting output.

## Out of scope
- The reviewer/Codex adapter (`adapters/codex.py`) already enforces a true
  wall-clock bound via `subprocess.run(timeout=)` + `TimeoutExpired` — do NOT
  touch it. This task only closes the asymmetry on the WORKER path.
- A "stall watchdog" variant (reset the timer on each line; kill after N seconds of
  *no* output, surfacing "worker silent for Ns" to `progress.md`) is a possible
  richer design, but is OUT of scope for this task — implement the simplest hard
  wall-clock deadline that matches the reviewer adapter's semantics. (If
  plan_review strongly prefers the watchdog, that is a scope change to raise
  explicitly, not to assume.)
- #112 / untracked-baseline work — unrelated, already merged.

## Affected files
- `.redteam/workflows/phase_runners/_base.py` — `run_claude` (the read loop /
  deadline / post-EOF wait region). Re-locate by symbol, not line number.
- New/extended test under `.redteam/tests/` (e.g. `test_run_claude_timeout.py`)
  for the regression.

## Verification
- `bash .redteam/scripts/verify.sh` (ruff check + ruff format --check + full
  pytest) stays green; the current suite must not regress. In particular the
  existing `test_run_claude_model.py` stubs (`_FakeProc` with an iterable
  `.stdout`, `.stderr`, `.returncode`, `.wait()`, `.kill()`) must keep passing —
  match that monkeypatch-`base.subprocess.Popen` style.
- A new deterministic, FAST regression test proves the hard bound: stub
  `subprocess.Popen` with a fake process whose `.stdout` yields ONE line and then
  blocks/sleeps well past a SMALL `timeout_sec` while holding stdout "open". Assert
  `run_claude` returns `returncode == 124` promptly (well under the fake's block
  duration) — i.e. it does NOT wait for the stream. The test must not actually
  sleep for the full default 900s and must not flake on a slow CI box (prefer
  injecting the timer/clock or a very small timeout over real long sleeps).
- A test asserting the normal path is unchanged: a fake proc that emits a
  `type:"result"` event and exits returns `returncode == 0` with `parsed_json`
  populated and the timer cancelled (no lingering timer / no 124).

## Risks
- Returning `proc.returncode` (SIGKILL `-9`) instead of `124` on a timer-kill would
  silently break every caller's `== 124` fail-closed branch — the timeout flag /
  124 mapping is the crux; cover it with the test above.
- A `Timer` not cancelled on the normal path leaks a thread and could later kill a
  reused process handle — cancel in `finally`.
- Exact line numbers in this brief are from current `main` and may shift; locate by
  symbol.
- This fix is dogfooded through the same worker path it changes; a mid-flight
  interrupt on the new test file can hit the (now-fixed, separately) #112 hazard —
  operational note only, not a scope change.
