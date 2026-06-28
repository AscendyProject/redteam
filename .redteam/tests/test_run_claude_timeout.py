"""Regression tests for the hard wall-clock deadline in run_claude (#109).

The threading.Timer must fire independent of stdout output — a process that
produces one line and then blocks forever must be killed at timeout_sec, NOT at
the next stdout line.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _load_base_module():
    import _engine

    return _engine.base()


# ---------------------------------------------------------------------------
# Fake process helpers
# ---------------------------------------------------------------------------


class _BlockingStdout:
    """Yields one line, then blocks until kill_event is set (proc.kill() called)."""

    def __init__(self, first_line: str, kill_event: threading.Event) -> None:
        self._first_line = first_line
        self._kill_event = kill_event
        self._yielded = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if not self._yielded:
            self._yielded = True
            return self._first_line
        # Block until the timer fires and calls proc.kill(), which sets the event.
        self._kill_event.wait()
        raise StopIteration

    def read(self) -> str:
        return ""


class _HangingProc:
    """Fake proc whose stdout yields one line then blocks until killed."""

    def __init__(self) -> None:
        self._kill_event = threading.Event()
        self.stdout = _BlockingStdout('{"type":"other"}\n', self._kill_event)
        self.stderr = io.StringIO("")
        self.returncode = -9  # what SIGKILL would produce
        self.kill_called = False

    def kill(self) -> None:
        self.kill_called = True
        self._kill_event.set()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _NormalProc:
    """Fake proc that emits a result event and exits cleanly (returncode=0)."""

    def __init__(self) -> None:
        self.stdout = io.StringIO('{"type":"result","is_error":false}\n')
        self.stderr = io.StringIO("")
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _ExceptionStdout:
    """Yields one line then raises OSError to simulate a stream read error."""

    def __iter__(self):
        yield '{"type":"other"}\n'
        raise OSError("fake stream error")

    def read(self) -> str:
        return ""


class _ExceptionProc:
    """Fake proc whose stdout raises mid-stream."""

    def __init__(self) -> None:
        self.stdout = _ExceptionStdout()
        self.stderr = io.StringIO("")
        self.returncode = -9
        self.kill_called = False

    def kill(self) -> None:
        self.kill_called = True

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _BoundedWaitProc:
    """Fake proc whose stdout closes immediately; records whether wait() received a timeout."""

    def __init__(self) -> None:
        self.stdout = io.StringIO("")  # EOF immediately
        self.stderr = io.StringIO("")
        self.returncode = 0
        self.wait_received_timeout = False

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_received_timeout = True
        return self.returncode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_silent_hang_returns_124(monkeypatch):
    """A fake proc that yields one line then blocks must be killed at timeout_sec
    and run_claude must return returncode=124 — NOT block waiting for stdout."""
    base = _load_base_module()
    proc = _HangingProc()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)

    start = time.monotonic()
    result = base.run_claude(agent="test-agent", prompt="x", timeout_sec=0.1)
    elapsed = time.monotonic() - start

    assert result["returncode"] == 124
    assert proc.kill_called
    # Must complete in well under the fake's indefinite block (5s is generous on any CI box)
    assert elapsed < 5.0


def test_timer_kill_returns_124_not_sigkill(monkeypatch):
    """The timer kill must return 124 (the fail-closed contract callers branch on),
    NOT proc.returncode (which is -9 from SIGKILL)."""
    base = _load_base_module()
    proc = _HangingProc()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)

    result = base.run_claude(agent="test-agent", prompt="x", timeout_sec=0.1)

    # Verify the distinction: proc.returncode is -9 (SIGKILL), result must be 124.
    assert proc.returncode == -9
    assert result["returncode"] == 124


def test_normal_path_returncode_and_parsed_json(monkeypatch):
    """Normal exit: returncode=0 and parsed_json populated from the result event."""
    base = _load_base_module()
    proc = _NormalProc()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)

    result = base.run_claude(agent="test-agent", prompt="x", timeout_sec=30)

    assert result["returncode"] == 0
    assert result["parsed_json"] is not None
    assert result["parsed_json"].get("type") == "result"


def test_normal_path_timer_is_cancelled(monkeypatch):
    """After a normal exit the timer must be cancelled — no lingering timer thread."""
    base = _load_base_module()
    proc = _NormalProc()

    real_timer = threading.Timer
    created: list[threading.Timer] = []

    class _SpyTimer(real_timer):  # type: ignore[misc]
        def __init__(self, interval, fn, *args, **kwargs):
            super().__init__(interval, fn, *args, **kwargs)
            self.daemon = True  # don't block process exit if something goes wrong
            self.cancel_was_called = False
            created.append(self)

        def cancel(self):
            self.cancel_was_called = True
            super().cancel()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(base.threading, "Timer", _SpyTimer)

    base.run_claude(agent="test-agent", prompt="x", timeout_sec=30)

    assert created, "run_claude did not create a threading.Timer"
    for t in created:
        assert t.cancel_was_called, "timer.cancel() was not called — potential thread leak"


def test_normal_path_wait_is_bounded(monkeypatch):
    """The post-EOF proc.wait() on the normal path must pass a timeout argument so a
    child that closes stdout but does not exit cannot hang run_claude indefinitely."""
    base = _load_base_module()
    proc = _BoundedWaitProc()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)

    result = base.run_claude(agent="test-agent", prompt="x", timeout_sec=30)

    assert result["returncode"] == 0
    assert proc.wait_received_timeout, "proc.wait() was called without a timeout — unbounded post-EOF wait"


def test_exception_path_returns_125_and_cancels_timer(monkeypatch):
    """When the stream read raises, run_claude returns returncode=125 and the timer
    is cancelled (no thread leak on the exception path)."""
    base = _load_base_module()
    proc = _ExceptionProc()

    real_timer = threading.Timer
    created: list[threading.Timer] = []

    class _SpyTimer(real_timer):  # type: ignore[misc]
        def __init__(self, interval, fn, *args, **kwargs):
            super().__init__(interval, fn, *args, **kwargs)
            self.daemon = True
            self.cancel_was_called = False
            created.append(self)

        def cancel(self):
            self.cancel_was_called = True
            super().cancel()

    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(base.threading, "Timer", _SpyTimer)

    result = base.run_claude(agent="test-agent", prompt="x", timeout_sec=30)

    assert result["returncode"] == 125
    assert created, "run_claude did not create a threading.Timer"
    for t in created:
        assert t.cancel_was_called, "timer.cancel() was not called on exception path — thread leak"
