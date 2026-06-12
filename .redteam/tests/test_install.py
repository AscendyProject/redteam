"""Vendoring installer (.redteam/scripts/install.py) contract.

Pins the data-loss invariant from the round-1 review: re-vendoring agent
skeletons must NOT delete a consumer's own unrelated agents under
.claude/agents, and project-owned files (config.toml, docs, verify.sh) are
seeded once and never overwritten — even with --overwrite.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("redteam_install", _SCRIPTS / "install.py")
assert _spec and _spec.loader
install_mod = importlib.util.module_from_spec(_spec)
sys.modules["redteam_install"] = install_mod
_spec.loader.exec_module(install_mod)


def test_fresh_install_vendors_and_seeds(tmp_path: Path) -> None:
    install_mod.install(tmp_path, overwrite=False, dry=False)
    # harness-owned engine vendored in
    assert (tmp_path / ".redteam/workflows/orchestrator.py").is_file()
    assert (tmp_path / ".redteam/scripts/install.py").is_file()
    # 6 agent skeletons placed file-by-file
    for name in install_mod.HARNESS_AGENTS:
        assert (tmp_path / f".claude/agents/{name}.md").is_file()
    # project-owned seeds: config from the template (placeholder, not redteam's own)
    cfg = (tmp_path / ".redteam/config.toml").read_text()
    assert 'name = "my-project"' in cfg
    assert (tmp_path / ".redteam/scripts/verify.sh").is_file()
    assert (tmp_path / ".redteam/batches/.gitkeep").is_file()
    # the harness's own tests are NOT shipped to a consumer
    assert not (tmp_path / ".redteam/tests").exists()


def test_overwrite_keeps_unrelated_consumer_agent(tmp_path: Path) -> None:
    """The round-1 HIGH: --overwrite must not delete a consumer's own agents."""
    install_mod.install(tmp_path, overwrite=False, dry=False)
    custom = tmp_path / ".claude/agents/my-custom-agent.md"
    custom.write_text("consumer's own agent")

    install_mod.install(tmp_path, overwrite=True, dry=False)

    assert custom.is_file()
    assert custom.read_text() == "consumer's own agent"
    # harness agents still present alongside it
    assert (tmp_path / ".claude/agents/implementer.md").is_file()


def test_overwrite_never_clobbers_project_owned(tmp_path: Path) -> None:
    install_mod.install(tmp_path, overwrite=False, dry=False)
    cfg = tmp_path / ".redteam/config.toml"
    cfg.write_text('[project]\nname = "edited-by-user"\n')
    verify = tmp_path / ".redteam/scripts/verify.sh"
    verify.write_text("echo my own gate")

    install_mod.install(tmp_path, overwrite=True, dry=False)

    assert 'name = "edited-by-user"' in cfg.read_text()
    assert verify.read_text() == "echo my own gate"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    install_mod.install(tmp_path, overwrite=False, dry=True)
    assert not (tmp_path / ".redteam").exists()
    assert not (tmp_path / ".claude").exists()


def test_refuses_self_install(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(SystemExit):
        install_mod.install(install_mod.SOURCE_ROOT, overwrite=False, dry=False)


# ---- config.toml protection: deny-rule merge into .claude/settings.json ----


def _settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))


def test_protect_config_off_by_default_leaves_settings_untouched(tmp_path: Path) -> None:
    """The deny-merge is OPT-IN: a default install (no --protect-config) must not
    create or touch .claude/settings.json. The runtime pairing guard is the
    backstop, so seeding consumer settings is the operator's explicit choice."""
    install_mod.install(tmp_path, overwrite=False, dry=False)
    assert not (tmp_path / ".claude/settings.json").exists()


def test_default_install_leaves_existing_settings_byte_for_byte(tmp_path: Path) -> None:
    """A pre-existing consumer .claude/settings.json must survive a default install
    (no --protect-config) completely unmodified — the deny-merge step is never
    reached, so the consumer-owned file is untouched byte-for-byte."""
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps({"model": "claude-opus-4-8", "permissions": {"deny": ["Bash(rm -rf:*)"]}})
    settings.write_text(original, encoding="utf-8")

    install_mod.install(tmp_path, overwrite=False, dry=False)

    assert settings.read_text(encoding="utf-8") == original


def test_fresh_install_seeds_config_deny_rules(tmp_path: Path) -> None:
    """With --protect-config, a fresh install protects config.toml: settings.json
    gets the Edit/Write deny rules so an agent can't silently rewrite the harness's
    model config."""
    install_mod.install(tmp_path, overwrite=False, dry=False, protect_config=True)
    deny = _settings(tmp_path)["permissions"]["deny"]
    for rule in install_mod.CONFIG_DENY_RULES:
        assert rule in deny


def test_settings_merge_preserves_existing_keys(tmp_path: Path) -> None:
    """settings.json is consumer-owned: the merge ADDS our rules without touching
    the consumer's other settings or their own deny entries."""
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"model": "claude-opus-4-8", "permissions": {"deny": ["Bash(rm -rf:*)"], "allow": ["Read(*)"]}}),
        encoding="utf-8",
    )

    install_mod.install(tmp_path, overwrite=False, dry=False, protect_config=True)

    data = _settings(tmp_path)
    assert data["model"] == "claude-opus-4-8"  # unrelated key preserved
    assert data["permissions"]["allow"] == ["Read(*)"]  # sibling list preserved
    assert "Bash(rm -rf:*)" in data["permissions"]["deny"]  # consumer's own rule preserved
    for rule in install_mod.CONFIG_DENY_RULES:
        assert rule in data["permissions"]["deny"]  # ours added alongside


def test_settings_merge_is_idempotent(tmp_path: Path) -> None:
    """Re-running install never duplicates the deny rules."""
    install_mod.install(tmp_path, overwrite=False, dry=False, protect_config=True)
    install_mod.install(tmp_path, overwrite=True, dry=False, protect_config=True)
    deny = _settings(tmp_path)["permissions"]["deny"]
    for rule in install_mod.CONFIG_DENY_RULES:
        assert deny.count(rule) == 1


def test_settings_merge_skips_malformed_json(tmp_path: Path, capsys) -> None:
    """A consumer's invalid settings.json is left byte-for-byte untouched rather
    than corrupted — fail safe, never clobber. The deny-merge step must run and
    hit its fail-safe branch (the emitted WARN proves it executed and skipped,
    not that the installer simply never looked at the file)."""
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not valid json", encoding="utf-8")

    install_mod.install(tmp_path, overwrite=False, dry=False, protect_config=True)

    assert settings.read_text(encoding="utf-8") == "{ not valid json"  # untouched
    # The deny-merge ran and chose to skip — distinguishes the new fail-safe code
    # path from the pre-feature installer, which never touched settings.json.
    err = capsys.readouterr().err
    assert install_mod.SETTINGS_REL in err and "deny-merge" in err


def test_dry_run_does_not_create_settings(tmp_path: Path, capsys) -> None:
    """Dry-run must not write settings.json (mirrors the no-.claude invariant),
    yet must still report the merge it WOULD do — proving the deny-merge step
    executed in dry mode and chose not to write, rather than being absent."""
    install_mod.install(tmp_path, overwrite=False, dry=True, protect_config=True)
    assert not (tmp_path / ".claude/settings.json").exists()
    # The dry-run still logs the planned settings.json deny-merge; the pre-feature
    # installer emitted no such line, so this fails against pre-change code.
    out = capsys.readouterr().out
    assert install_mod.SETTINGS_REL in out and "deny rule" in out
