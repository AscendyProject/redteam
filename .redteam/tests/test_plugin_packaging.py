"""Guards the Claude Code plugin packaging of THIS repo (issue #8, Option A).

This repo doubles as a single-plugin marketplace: `.claude-plugin/{plugin.json,
marketplace.json}` + `bin/redteam-install` (a PATH wrapper around the bundled
installer) + `commands/redteam-install.md`. The vendored-copy model is unchanged
— the plugin only delivers + installs the harness; the engine still resolves the
repo root from its own location once vendored.

These files live OUTSIDE the harness-owned trees install.py vendors, and this
test lives outside them too (`.redteam/tests/` is not in install.py's copy
lists), so none of this reaches a consumer repo. The asserts pin the packaging
contract and catch drift between the marketplace agents override and the agent
set install.py actually vendors.
"""

import json
import stat
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_JSON = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _ROOT / ".claude-plugin" / "marketplace.json"
_BIN = _ROOT / "bin" / "redteam-install"
_COMMAND = _ROOT / "commands" / "redteam-install.md"

# The six generic sub-agents install.py vendors (kept in sync with
# install.py HARNESS_AGENTS — drift here is the bug this guards).
_EXPECTED_AGENTS = {
    "code-security-reviewer",
    "implementer",
    "outcome-planner",
    "pr-author",
    "test-author",
    "test-verifier",
}


def test_plugin_manifest_valid():
    """plugin.json is well-formed with the fields Claude Code requires/expects."""
    data = json.loads(_PLUGIN_JSON.read_text())
    assert data["name"] == "redteam"
    assert data["name"] == data["name"].lower().replace(" ", "")  # kebab/lower
    assert data["version"]  # non-empty
    assert data["license"] == "AGPL-3.0-or-later"  # matches repo LICENSE


def test_marketplace_lists_this_repo_as_the_plugin():
    """marketplace.json self-serves: one plugin sourced at the repo root."""
    data = json.loads(_MARKETPLACE_JSON.read_text())
    assert data["name"]
    assert data["owner"]["name"]
    plugins = data["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == "redteam"
    assert entry["source"] == "."  # the plugin is this same repo root


def test_marketplace_agents_override_points_at_the_vendored_agents():
    """The agents override must resolve to the real dir holding the six skeletons
    install.py vendors — guards drift between packaging and the installer."""
    data = json.loads(_MARKETPLACE_JSON.read_text())
    agents_paths = data["plugins"][0]["agents"]
    assert isinstance(agents_paths, list) and agents_paths
    resolved = (_ROOT / agents_paths[0]).resolve()
    assert resolved.is_dir(), f"agents override path missing: {resolved}"
    on_disk = {p.stem for p in resolved.glob("*.md")}
    assert _EXPECTED_AGENTS <= on_disk, f"missing agents: {_EXPECTED_AGENTS - on_disk}"


def test_install_wrapper_is_executable_and_self_locating():
    """bin/redteam-install ships +x, self-locates from $0 (not $CLAUDE_PLUGIN_ROOT,
    which isn't guaranteed in command bodies), and execs the bundled installer."""
    assert _BIN.exists()
    assert _BIN.stat().st_mode & stat.S_IXUSR, "bin/redteam-install must be executable"
    body = _BIN.read_text()
    assert "BASH_SOURCE" in body  # resolves its own path
    assert ".redteam/scripts/install.py" in body  # targets the real installer
    assert "exec python3" in body


def test_install_command_has_frontmatter():
    """The slash command carries a description so Claude Code surfaces it."""
    body = _COMMAND.read_text()
    assert body.startswith("---")
    assert "description:" in body.split("---", 2)[1]
