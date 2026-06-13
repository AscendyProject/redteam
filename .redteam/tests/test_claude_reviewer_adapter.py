"""ClaudeReviewerAdapter — headless read-only review via `claude -p`.

The model-freedom registry gains a Claude reviewer (so `reviewer="claude"`
works, not just codex). It mirrors the codex adapter's fail-closed contract —
non-zero exit, timeout, missing binary, or unparseable output → MISSING/error,
never a silent approval — and enforces read-only via `--permission-mode plan`
(the codex `--sandbox read-only` equivalent). Claude-specific: it parses the
final text from `--output-format json` (the `.result` field).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from adapters.claude import ClaudeReviewerAdapter  # noqa: E402

_TARGET = {"kind": "branch_diff", "base": "main"}


def _fake_proc(returncode: int, stdout: str, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _claude_json(result_text: str) -> str:
    return json.dumps({"result": result_text, "session_id": "x", "total_cost_usd": 0.0})


def test_parses_decision_from_json_result() -> None:
    fake = _fake_proc(0, _claude_json("work...\nREVIEW_DECISION: APPROVED"))
    with patch("adapters.claude.subprocess.run", return_value=fake):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["decision"] == "APPROVED"
    assert r["parse_status"] == "ok"
    assert "REVIEW_DECISION: APPROVED" in r["raw"]


def test_decodes_as_utf8() -> None:
    """#32: subprocess.run must pin encoding="utf-8" so reviewer output with
    non-ASCII decodes consistently instead of crashing on a non-UTF-8 platform
    default (e.g. cp949 on Korean Windows)."""
    fake = _fake_proc(0, _claude_json("REVIEW_DECISION: APPROVED"))
    with patch("adapters.claude.subprocess.run", return_value=fake) as run:
        ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert run.call_args.kwargs["encoding"] == "utf-8"


def test_missing_decision_unparseable() -> None:
    with patch("adapters.claude.subprocess.run", return_value=_fake_proc(0, _claude_json("no decision here"))):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["decision"] == "MISSING"
    assert r["parse_status"] == "unparseable"


def test_unparseable_json_fails_closed() -> None:
    """claude stdout that isn't valid JSON must fail closed, not approve."""
    with patch("adapters.claude.subprocess.run", return_value=_fake_proc(0, "not json at all")):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["decision"] == "MISSING"
    assert r["parse_status"] == "error"


def test_non_string_result_fails_closed() -> None:
    """Valid JSON whose `result` is null / not a string (or a non-object payload)
    must fail closed — it must never reach parse_review_decision, which would
    raise rather than return MISSING (Codex review finding)."""
    for bad in (
        json.dumps({"result": None}),
        json.dumps({"result": 123}),
        json.dumps({"no_result": "x"}),
        json.dumps(["a", "list"]),
        json.dumps("just a string"),
    ):
        with patch("adapters.claude.subprocess.run", return_value=_fake_proc(0, bad)):
            r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
        assert r["decision"] == "MISSING", bad
        assert r["parse_status"] == "error", bad


def test_not_found_is_error() -> None:
    with patch("adapters.claude.subprocess.run", side_effect=FileNotFoundError()):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"
    assert "claude" in r["raw"].lower()


def test_timeout_fails_closed() -> None:
    with patch(
        "adapters.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    ):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"


def test_nonzero_exit_fails_closed_and_omits_stderr() -> None:
    """Non-zero exit fails closed even if stdout ends in APPROVED; stderr (which
    can carry an auth token) must NOT appear in raw."""
    fake = _fake_proc(
        1,
        _claude_json("partial\nREVIEW_DECISION: APPROVED"),
        stderr="Authorization: Bearer sk-SECRETTOKEN123",
    )
    with patch("adapters.claude.subprocess.run", return_value=fake):
        r = ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    assert r["parse_status"] == "error"
    assert r["decision"] == "MISSING"
    assert "sk-SECRETTOKEN123" not in r["raw"]


def test_declares_capabilities() -> None:
    caps = ClaudeReviewerAdapter.capabilities
    assert caps["native_diff_review"] is False
    assert caps["timeout_sec"] >= 1


def test_read_only_invocation_uses_plan_mode() -> None:
    """The command must enforce read-only: --permission-mode plan + json output,
    and must not grant Edit/Write."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(0, _claude_json("REVIEW_DECISION: APPROVED"))

    with patch("adapters.claude.subprocess.run", side_effect=fake_run):
        ClaudeReviewerAdapter().review(role="review_code", prompt="x", cwd=Path("."), target=_TARGET)
    cmd = captured["cmd"]
    assert "--permission-mode" in cmd and "plan" in cmd
    assert "--output-format" in cmd and "json" in cmd
    # read-only: mutating tools explicitly disallowed
    assert "--disallowedTools" in cmd
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "Edit" in disallowed and "Write" in disallowed
    # read-only git only — not a broad Bash(git *) that would allow commit/checkout
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "git diff" in allowed
    assert "Bash(git *)" not in allowed
