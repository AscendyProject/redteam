"""Tests for orchestrator.py config subcommand wiring."""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

# These imports follow the sys.path insert above so workflows directory resolves.
import config_cli  # noqa: E402
import orchestrator  # noqa: E402


def _make_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / ".redteam" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    return config_path


def test_config_dispatched_by_main_without_batch(monkeypatch) -> None:
    """(a) dispatch: main(["orchestrator.py", "config"]) routes to cmd_config."""
    called = {"config": False}

    def _fake_config():
        called["config"] = True
        return 42

    monkeypatch.setattr(orchestrator, "cmd_config", _fake_config)
    assert orchestrator.main(["orchestrator.py", "config"]) == 42
    assert called["config"] is True


def test_cmd_config_injects_real_guard(monkeypatch, tmp_path) -> None:
    """(b) guard-injection identity: run_config receives the real guard function."""
    captured_guard = None

    def _fake_run_config(repo, pairing_guard):
        nonlocal captured_guard
        captured_guard = pairing_guard
        return 0

    monkeypatch.setattr(config_cli, "run_config", _fake_run_config)

    rc = orchestrator.cmd_config(repo=tmp_path)
    assert rc == 0
    assert captured_guard is orchestrator._adversarial_pairing_error


def test_cmd_config_exit_code_passthrough(monkeypatch, tmp_path) -> None:
    """(c) exit-code passthrough: cmd_config returns config_cli.run_config exit code unchanged."""
    for code in (0, 1, 42):
        monkeypatch.setattr(config_cli, "run_config", lambda repo, guard: code)
        assert orchestrator.cmd_config(repo=tmp_path) == code


def test_cmd_config_repo_defaulting(monkeypatch, tmp_path) -> None:
    """(d) repo defaulting: repo resolves via repo_root() when None, else remains explicit."""
    called_repo_root = False
    passed_repo = None

    def _fake_repo_root():
        nonlocal called_repo_root
        called_repo_root = True
        return Path("/fake/root")

    def _fake_run_config(repo, guard):
        nonlocal passed_repo
        passed_repo = repo
        return 0

    monkeypatch.setattr(orchestrator, "repo_root", _fake_repo_root)
    monkeypatch.setattr(config_cli, "run_config", _fake_run_config)

    # 1. repo is None -> calls repo_root()
    rc = orchestrator.cmd_config(repo=None)
    assert rc == 0
    assert called_repo_root is True
    assert passed_repo == Path("/fake/root")

    # Reset
    called_repo_root = False
    passed_repo = None

    # 2. repo is explicit -> does not call repo_root()
    rc = orchestrator.cmd_config(repo=tmp_path)
    assert rc == 0
    assert called_repo_root is False
    assert passed_repo == tmp_path


def test_cmd_config_real_guard_refusal_e2e(monkeypatch, tmp_path) -> None:
    """(e) end-to-end REAL-guard refusal: self-review-collapse is refused and file is unchanged."""
    config_content = """\
[project]
name = "test-proj"
verify_command = "bash verify.sh"

[models]
planner = "claude-opus-4-7"
implementer = "claude-sonnet-4-6"
reviewer = "claude"  # Same provider as implementer (claude)
rescue = "codex"
"""
    config_path = _make_config(tmp_path, config_content)

    # Mock inputs to keep the current on-disk values (which trigger self-review collapse)
    input_values = ["", "", "", ""]
    input_iter = iter(input_values)
    monkeypatch.setattr("builtins.input", lambda _: next(input_iter))

    rc = orchestrator.cmd_config(repo=tmp_path)
    assert rc != 0
    assert config_path.read_text(encoding="utf-8") == config_content


def test_usage_and_docstring_surface() -> None:
    """(f) USAGE/docstring surface: usage string and docstring contain the config command."""
    assert "config" in orchestrator.USAGE
    assert "config" in (orchestrator.__doc__ or "")
