Disagree

IR-001 severity:major status:open

`run_claude` can still escape without a `ClaudeRunResult` when the child closes stdout but stays alive. The new normal path calls `proc.wait(timeout=5)` at `.redteam/workflows/phase_runners/_base.py:314`, but it does not catch `subprocess.TimeoutExpired`. That means a worker that reaches EOF on stdout and then hangs exits `run_claude` by exception after 5s instead of returning the fail-closed result shape callers expect. The task explicitly called out the post-EOF wait as a remaining hang point, and the caller contract remains returncode-based.

The new test intended to cover this does not exercise the failing path: `_BoundedWaitProc.wait()` only records that a timeout argument was passed and returns `0` at `.redteam/tests/test_run_claude_timeout.py:127-130`, so `.redteam/tests/test_run_claude_timeout.py:214-225` would stay green even if real `Popen.wait(timeout=5)` would raise for a still-running child. Add a fake whose `wait(timeout=...)` raises `subprocess.TimeoutExpired` and make `run_claude` handle it fail-closed, likely by killing and returning `124` with the established timeout stderr shape.

Uncertain

None.

Agree

The core silent-stdout bug is addressed for the main stream-read case: `.redteam/workflows/phase_runners/_base.py:270-276` installs a `threading.Event` plus `threading.Timer`, `.redteam/workflows/phase_runners/_base.py:282-288` reads stdout without relying on a new line for timeout checks, and `.redteam/workflows/phase_runners/_base.py:299-312` maps a timer-fired kill to `returncode=124` instead of `proc.returncode`.

The timer is cancelled in a `finally` at `.redteam/workflows/phase_runners/_base.py:323-324`, and the FileNotFoundError, stream-read `125`, UTF-8, `bufsize=1`, stdout/stderr PIPE, and parsed result-event behavior remain intact.

The new tests are mostly discriminating: `test_silent_hang_returns_124` would hang/fail against the old implementation because there was no independent timer to unblock `_BlockingStdout`; `test_timer_kill_returns_124_not_sigkill` pins the `124` contract; normal parsed JSON and timer-cancel tests preserve existing behavior. Verification artifacts exist, and `state.json` reports `verification.last_exit_code == 0`; `verification.log` reports `bash .redteam/scripts/verify.sh` passed with 474 tests.

REVIEW_DECISION: CHANGES_REQUESTED
