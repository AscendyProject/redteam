Disagree

IR-002 severity:major status:open

`run_claude` can still return `proc.returncode` instead of the required fail-closed `124` when the wall-clock timer fires during the bounded post-EOF wait. The code checks `timed_out.is_set()` once at `.redteam/workflows/phase_runners/_base.py:299`, then enters `proc.wait(timeout=5)` at line 315. If the child has closed stdout but remains alive, the timer can fire while that wait is blocking, `_kill_on_timeout()` sets the event and kills the process at lines 272-274, `wait()` returns the killed process status, and the function falls through to returning `proc.returncode` at lines 332-337. That violates the outcome’s explicit contract that a deadline-fired kill returns `ClaudeRunResult(returncode=124, ...)`, not SIGKILL `-9`.

The new tests miss this race. `.redteam/tests/test_run_claude_timeout.py:252-264` covers a post-EOF `wait(timeout=...)` that raises `TimeoutExpired` immediately, but not a wait that is interrupted by the timer’s kill and returns `-9`. Add a fake whose stdout EOFs, whose `wait(timeout=5)` blocks until `kill()` is called by the timer, and assert the result is `124`.

Uncertain

None.

Agree

IR-001 severity:major status:resolved

The prior post-EOF `TimeoutExpired` finding was addressed: `.redteam/workflows/phase_runners/_base.py:314-329` now catches `subprocess.TimeoutExpired`, kills the child, and returns the established `124` timeout shape. The regression test at `.redteam/tests/test_run_claude_timeout.py:252-264` would have failed against the previous round because `TimeoutExpired` propagated.

The core silent-stdout deadline is improved: `.redteam/workflows/phase_runners/_base.py:270-276` installs an in-process `threading.Event` plus `threading.Timer`, the stdout loop no longer depends on a fresh line to notice elapsed time, and `.redteam/workflows/phase_runners/_base.py:299-312` maps the timer-fired path to `124`. The timer is cancelled in `finally` at lines 338-339. `FileNotFoundError`, UTF-8 text capture, `bufsize=1`, stdout/stderr pipes, parsed result-event capture, and the stream-read `125` branch are preserved.

Verification artifacts exist. `state.json` reports `verification.last_exit_code == 0`, and `verification.log` reports `bash .redteam/scripts/verify.sh` passed with ruff and 475 pytest tests.

REVIEW_DECISION: CHANGES_REQUESTED
