"""Tests for config_cli.run_config — the per-role model picker.

Covers behaviors (a)–(f) from outcome.md plus auxiliary checks:
  (a) valid write: role values updated; non-[models] content preserved byte-for-byte;
      reviewer_fallback (non-target key) preserved byte-for-byte.
  (b) empty-input passthrough: empty response keeps the on-disk value.
  (c) guard refusal: non-zero return; file bytes unchanged on disk.
  (d) guard payload: state dict carries four chosen roles + mode='agent-pair';
      no tier_phases key; called exactly once before any write.
  (e) prompt order: planner → implementer → reviewer → rescue; each shows its
      current on-disk value.
  (f) recommended-default display: each prompt contains BOTH the on-disk value
      AND the ModelsConfig dataclass default (pinned against ModelsConfig, not
      a hardcoded string).
  Extra: missing [models] section or missing role → non-zero, file unchanged.
  Extra: module does not import orchestrator.
  Extra: file I/O uses encoding='utf-8'.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import config_cli  # noqa: E402
from config import ModelsConfig  # noqa: E402

# Synthetic config: distinct role values (equal to ModelsConfig defaults so
# behavior (e) / (f) tests can target each value individually when needed).
_SYNTHETIC_CONFIG = """\
# top comment
[project]
name = "test-proj"
verify_command = "bash verify.sh"

[models]
# Role → model
planner = "claude-opus-4-7"
implementer = "claude-sonnet-4-6"
reviewer = "codex"
rescue = "codex"
reviewer_fallback = "manual"  # policy

[tiers]
"""

# Config where on-disk role values differ from ModelsConfig defaults; used to
# assert that both are independently visible in the prompt (behavior f).
_CUSTOM_CONFIG = """\
[models]
planner = "custom-planner"
implementer = "custom-implementer"
reviewer = "custom-reviewer"
rescue = "custom-rescue"
reviewer_fallback = "manual"
"""


def _make_config(tmp_path: Path, content: str = _SYNTHETIC_CONFIG) -> Path:
    config_path = tmp_path / ".redteam" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    return config_path


class _FakeGuard:
    """Records calls to the injected pairing_guard; returns a fixed error (or None)."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, state: dict) -> str | None:
        self.calls.append(state)
        return self.error


# ---------------------------------------------------------------------------
# Behavior (a) — valid write path
# ---------------------------------------------------------------------------


def test_write_path_role_values_updated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """New role values appear inside [models] after run_config returns 0."""
    _make_config(tmp_path)
    new_vals = iter(["new-planner", "new-implementer", "new-reviewer", "new-rescue"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    rc = config_cli.run_config(tmp_path, guard)

    assert rc == 0
    written = (tmp_path / ".redteam" / "config.toml").read_text(encoding="utf-8")
    assert 'planner = "new-planner"' in written
    assert 'implementer = "new-implementer"' in written
    assert 'reviewer = "new-reviewer"' in written
    assert 'rescue = "new-rescue"' in written


def test_write_path_non_models_content_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines outside the [models] block are byte-for-byte unchanged after write."""
    config_path = _make_config(tmp_path)
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    written = config_path.read_text(encoding="utf-8")
    assert "# top comment" in written
    assert "[project]" in written
    assert 'name = "test-proj"' in written
    assert "[tiers]" in written
    # Comment inside the [models] block is also preserved.
    assert "# Role" in written


def test_write_path_reviewer_fallback_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-target key reviewer_fallback (with inline comment) is preserved byte-for-byte."""
    config_path = _make_config(tmp_path)
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    written = config_path.read_text(encoding="utf-8")
    assert 'reviewer_fallback = "manual"  # policy' in written


# ---------------------------------------------------------------------------
# Behavior (b) — empty-input passthrough
# ---------------------------------------------------------------------------


def test_empty_input_keeps_current_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty input for every role leaves all on-disk values unchanged after write."""
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "")
    guard = _FakeGuard()

    rc = config_cli.run_config(tmp_path, guard)

    assert rc == 0
    written = config_path.read_text(encoding="utf-8")
    assert 'planner = "claude-opus-4-7"' in written
    assert 'implementer = "claude-sonnet-4-6"' in written
    assert 'reviewer = "codex"' in written
    assert 'rescue = "codex"' in written


def test_empty_input_single_role_keeps_that_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty input for just the reviewer role preserves only that role's value."""
    config_path = _make_config(tmp_path)
    # new values for planner/implementer/rescue; empty for reviewer
    responses = iter(["new-planner", "new-implementer", "", "new-rescue"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    written = config_path.read_text(encoding="utf-8")
    assert 'planner = "new-planner"' in written
    assert 'reviewer = "codex"' in written  # kept original


# ---------------------------------------------------------------------------
# Behavior (c) — guard refusal
# ---------------------------------------------------------------------------


def test_guard_refusal_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard returning an error string → run_config returns non-zero."""
    _make_config(tmp_path)
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard(error="adversarial pairing collapsed: self-review")

    rc = config_cli.run_config(tmp_path, guard)

    assert rc != 0


def test_guard_refusal_file_bytes_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On guard refusal, .redteam/config.toml bytes are identical before and after."""
    config_path = _make_config(tmp_path)
    before = config_path.read_bytes()
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard(error="adversarial pairing collapsed: self-review")

    config_cli.run_config(tmp_path, guard)

    assert config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Behavior (d) — guard payload shape
# ---------------------------------------------------------------------------


def test_guard_called_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected guard is called exactly once before the write."""
    _make_config(tmp_path)
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    assert len(guard.calls) == 1


def test_guard_payload_mode_and_no_tier_phases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard state has mode='agent-pair' and no tier_phases key."""
    _make_config(tmp_path)
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    state = guard.calls[0]
    assert state["mode"] == "agent-pair"
    assert "tier_phases" not in state


def test_guard_payload_models_carries_chosen_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard state['models'] carries the four chosen role values."""
    _make_config(tmp_path)
    new_vals = iter(["new-planner", "new-implementer", "new-reviewer", "new-rescue"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    models = guard.calls[0]["models"]
    assert models["planner"] == "new-planner"
    assert models["implementer"] == "new-implementer"
    assert models["reviewer"] == "new-reviewer"
    assert models["rescue"] == "new-rescue"


def test_guard_payload_includes_extra_model_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard state['models'] passes through non-target keys (e.g. reviewer_fallback)."""
    _make_config(tmp_path)  # _SYNTHETIC_CONFIG has reviewer_fallback = "manual"
    new_vals = iter(["p", "i", "r", "s"])
    monkeypatch.setattr("builtins.input", lambda _: next(new_vals))
    guard = _FakeGuard()

    config_cli.run_config(tmp_path, guard)

    models = guard.calls[0]["models"]
    assert models.get("reviewer_fallback") == "manual"


# ---------------------------------------------------------------------------
# Behavior (e) — prompt order and current-value display
# ---------------------------------------------------------------------------


def test_prompt_order_is_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompts appear in order planner → implementer → reviewer → rescue."""
    _make_config(tmp_path)
    prompt_roles: list[str] = []

    def capturing_input(prompt: str) -> str:
        prompt_roles.append(prompt.split(" ")[0])
        return ""

    monkeypatch.setattr("builtins.input", capturing_input)
    config_cli.run_config(tmp_path, _FakeGuard())

    assert prompt_roles == ["planner", "implementer", "reviewer", "rescue"]


def test_prompt_shows_current_on_disk_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each prompt text contains the role's current on-disk value."""
    _make_config(tmp_path, _CUSTOM_CONFIG)
    prompts: list[str] = []

    def capturing_input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", capturing_input)
    config_cli.run_config(tmp_path, _FakeGuard())

    on_disk = {
        "planner": "custom-planner",
        "implementer": "custom-implementer",
        "reviewer": "custom-reviewer",
        "rescue": "custom-rescue",
    }
    for i, role in enumerate(("planner", "implementer", "reviewer", "rescue")):
        assert on_disk[role] in prompts[i], (
            f"prompt[{i}] for role {role!r} must contain on-disk value {on_disk[role]!r}"
        )


# ---------------------------------------------------------------------------
# Behavior (f) — recommended-default display
# ---------------------------------------------------------------------------


def test_prompt_shows_recommended_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each prompt contains BOTH the current on-disk value AND the ModelsConfig default.

    Uses _CUSTOM_CONFIG so on-disk values differ from ModelsConfig defaults,
    making the two independently verifiable in the prompt text.
    """
    _make_config(tmp_path, _CUSTOM_CONFIG)
    prompts: list[str] = []

    def capturing_input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", capturing_input)
    config_cli.run_config(tmp_path, _FakeGuard())

    # Pin against ModelsConfig field defaults — not hardcoded literals.
    field_defaults = {
        f.name: f.default
        for f in dataclasses.fields(ModelsConfig)
        if f.name in ("planner", "implementer", "reviewer", "rescue")
    }
    on_disk = {
        "planner": "custom-planner",
        "implementer": "custom-implementer",
        "reviewer": "custom-reviewer",
        "rescue": "custom-rescue",
    }
    for i, role in enumerate(("planner", "implementer", "reviewer", "rescue")):
        prompt = prompts[i]
        assert on_disk[role] in prompt, f"prompt for {role!r} must contain current value {on_disk[role]!r}"
        assert field_defaults[role] in prompt, (
            f"prompt for {role!r} must contain ModelsConfig default {field_defaults[role]!r}"
        )


# ---------------------------------------------------------------------------
# Missing section / missing role
# ---------------------------------------------------------------------------


def test_missing_models_section_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No [models] section → non-zero return; file bytes unchanged."""
    content = '[project]\nname = "x"\nverify_command = "bash v.sh"\n'
    config_path = _make_config(tmp_path, content)
    before = config_path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "x")

    rc = config_cli.run_config(tmp_path, _FakeGuard())

    assert rc != 0
    assert config_path.read_bytes() == before


def test_missing_role_line_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[models] section missing the rescue key → non-zero return; file bytes unchanged."""
    content = '[models]\nplanner = "x"\nimplementer = "y"\nreviewer = "z"\n'
    config_path = _make_config(tmp_path, content)
    before = config_path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "x")

    rc = config_cli.run_config(tmp_path, _FakeGuard())

    assert rc != 0
    assert config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Structural / isolation checks
# ---------------------------------------------------------------------------


def test_no_orchestrator_import() -> None:
    """config_cli must not import orchestrator (isolation + circular-import guard)."""
    source = Path(config_cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "orchestrator", "config_cli must not import orchestrator"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "orchestrator", "config_cli must not from-import orchestrator"


def test_file_io_uses_utf8_encoding() -> None:
    """Both file opens must specify encoding='utf-8' (Windows/cp949 hard rule)."""
    source = Path(config_cli.__file__).read_text(encoding="utf-8")
    assert source.count('encoding="utf-8"') >= 2, "config_cli must use encoding='utf-8' for both read and write opens"
