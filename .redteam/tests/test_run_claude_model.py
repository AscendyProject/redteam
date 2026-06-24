from __future__ import annotations

import io
import sys
from pathlib import Path

# _base.default_model_for_role lazily imports `config`; ensure the workflows dir
# is importable when these tests drive it directly.
_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _load_base_module():
    import _engine

    return _engine.base()


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = io.StringIO('{"type":"result","is_error":false}\n')
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout: int | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_run_claude_passes_model_when_configured(monkeypatch):
    base = _load_base_module()
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    result = base.run_claude(agent="implementer", prompt="do it", model="claude-sonnet-4-6")

    assert result["returncode"] == 0
    assert captured["cmd"][1:3] == ["--model", "claude-sonnet-4-6"]


def test_run_claude_pins_utf8_encoding(monkeypatch):
    """#32: the streaming Popen must pin encoding="utf-8" so non-ASCII worker
    output doesn't raise UnicodeDecodeError on a non-UTF-8 platform default
    (e.g. cp949 on Korean Windows) while iterating the stream."""
    base = _load_base_module()
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    base.run_claude(agent="implementer", prompt="do it", model=None)

    assert captured["kwargs"]["encoding"] == "utf-8"


def test_run_claude_omits_model_when_none(monkeypatch):
    base = _load_base_module()
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    result = base.run_claude(agent="implementer", prompt="do it", model=None)

    assert result["returncode"] == 0
    assert "--model" not in captured["cmd"]


def test_claude_model_for_role_ignores_codex_owner():
    base = _load_base_module()

    state = {"models": {"reviewer": "codex", "implementer": "claude-sonnet-4-6"}}

    assert base.claude_model_for_role(state, "reviewer") is None
    assert base.claude_model_for_role(state, "implementer") == "claude-sonnet-4-6"


def test_default_model_for_role_reads_config_defaults(monkeypatch, tmp_path):
    """No config.toml → ModelsConfig defaults (the former DEFAULT_MODELS values)."""
    base = _load_base_module()
    monkeypatch.setattr(base, "repo_root", lambda: tmp_path)
    assert base.default_model_for_role("planner") == "claude-opus-4-7"
    assert base.default_model_for_role("implementer") == "claude-sonnet-4-6"
    assert base.default_model_for_role("reviewer") == "codex"
    assert base.default_model_for_role("rescue") == "codex"


def test_default_model_for_role_honors_config_override(monkeypatch, tmp_path):
    """The model-freedom seam: a project picks its own role→model in config.toml."""
    base = _load_base_module()
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[models]\nplanner = "gpt-5"\nreviewer = "claude-opus-4-8"\n')
    monkeypatch.setattr(base, "repo_root", lambda: tmp_path)
    assert base.default_model_for_role("planner") == "gpt-5"
    assert base.default_model_for_role("reviewer") == "claude-opus-4-8"
    # unspecified keys keep their defaults
    assert base.default_model_for_role("implementer") == "claude-sonnet-4-6"


def test_claude_model_for_role_falls_back_to_config(monkeypatch, tmp_path):
    """With no state.models, the role resolves to the config default; a codex
    owner still maps to None (not a valid `claude --model`)."""
    base = _load_base_module()
    monkeypatch.setattr(base, "repo_root", lambda: tmp_path)
    assert base.claude_model_for_role({}, "implementer") == "claude-sonnet-4-6"
    assert base.claude_model_for_role({}, "reviewer") is None


def _captured_permission_mode(cmd: list[str]) -> str:
    """Pull the value following --permission-mode out of the spawned argv."""
    i = cmd.index("--permission-mode")
    return cmd[i + 1]


def test_worker_permission_mode_defaults_to_bypass(monkeypatch):
    """No env override → _worker_permission_mode() returns the historical
    bypassPermissions default (the unattended-batch default). Asserts the new
    helper directly: pre-change there was no helper (run_claude hard-coded the
    literal), so this fails against pre-change code where the name is undefined.
    The override test below pins that run_claude actually wires this value into
    the spawned argv."""
    base = _load_base_module()
    monkeypatch.delenv("REDTEAM_CLAUDE_PERMISSION_MODE", raising=False)
    assert base._worker_permission_mode() == "bypassPermissions"


def test_run_claude_honors_permission_mode_env_override(monkeypatch):
    """REDTEAM_CLAUDE_PERMISSION_MODE lets a locked-down environment (e.g.
    enterprise managed settings that refuse bypassPermissions) pick a
    policy-compatible mode like acceptEdits."""
    base = _load_base_module()
    monkeypatch.setenv("REDTEAM_CLAUDE_PERMISSION_MODE", "acceptEdits")
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    base.run_claude(agent="implementer", prompt="do it", model=None)

    assert _captured_permission_mode(captured["cmd"]) == "acceptEdits"


def test_run_claude_rejects_unknown_permission_mode(monkeypatch):
    """A typo'd / unsupported mode must fail loud rather than silently weaken
    (or break) the gate by passing an unrecognized value through to the CLI."""
    import pytest

    base = _load_base_module()
    monkeypatch.setenv("REDTEAM_CLAUDE_PERMISSION_MODE", "yolo")

    def fake_popen(cmd, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("Popen should not run when the mode is invalid")

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    with pytest.raises(ValueError, match="REDTEAM_CLAUDE_PERMISSION_MODE"):
        base.run_claude(agent="implementer", prompt="do it", model=None)


def test_worker_allowed_tools_empty_by_default(monkeypatch):
    """No REDTEAM_CLAUDE_ALLOWED_TOOLS → _worker_allowed_tools() returns [], so
    run_claude adds no --allowedTools flag (behavior unchanged). Asserts the new
    helper directly: pre-change there was no helper or flag, so this fails against
    pre-change code where the name is undefined. The passes-when-set test below
    pins that a non-empty value reaches the spawned argv."""
    base = _load_base_module()
    monkeypatch.delenv("REDTEAM_CLAUDE_ALLOWED_TOOLS", raising=False)
    assert base._worker_allowed_tools() == []


def test_run_claude_passes_allowed_tools_when_set(monkeypatch):
    """REDTEAM_CLAUDE_ALLOWED_TOOLS pre-approves shell tools so the worker can
    self-verify (run ruff/pytest) under a non-bypass mode like acceptEdits.
    Values are injected before --output-format so the CLI stops consuming them
    at that flag."""
    base = _load_base_module()
    monkeypatch.setenv("REDTEAM_CLAUDE_ALLOWED_TOOLS", "Bash, PowerShell")
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)

    base.run_claude(agent="implementer", prompt="do it", model=None)

    cmd = captured["cmd"]
    i = cmd.index("--allowedTools")
    assert cmd[i + 1 : i + 3] == ["Bash", "PowerShell"]
    assert cmd[i + 3] == "--output-format"
