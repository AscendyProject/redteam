"""Vendoring installer (.redteam/scripts/install.py) contract.

Pins the data-loss invariant from the round-1 review: re-vendoring agent
skeletons must NOT delete a consumer's own unrelated agents under
.claude/agents, and project-owned files (config.toml, docs, verify.sh) are
seeded once and never overwritten — even with --overwrite.
"""

from __future__ import annotations

import importlib.util
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
