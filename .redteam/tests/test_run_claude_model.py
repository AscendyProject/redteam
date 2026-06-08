from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

# _base.default_model_for_role lazily imports `config`; ensure the workflows dir
# is importable when these tests drive it directly.
_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _load_base_module():
    module_path = Path(__file__).resolve().parents[1] / "workflows" / "phase_runners" / "_base.py"
    spec = importlib.util.spec_from_file_location("redteam_phase_base", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
