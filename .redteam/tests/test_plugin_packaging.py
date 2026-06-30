"""Guards the Claude Code plugin packaging of THIS repo (issue #8, Option A).

This repo doubles as a single-plugin marketplace: `.claude-plugin/{plugin.json,
marketplace.json}` + `bin/redteam-install` (a PATH wrapper around the bundled
installer) + `commands/install.md`. The vendored-copy model is unchanged
— the plugin only delivers + installs the harness; the engine still resolves the
repo root from its own location once vendored.

These files live OUTSIDE the harness-owned trees install.py vendors, and this
test lives outside them too (`.redteam/tests/` is not in install.py's copy
lists), so none of this reaches a consumer repo. The asserts pin the packaging
contract and catch drift between the marketplace agents override and the agent
set install.py actually vendors.
"""

import json
import os
import stat
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_JSON = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _ROOT / ".claude-plugin" / "marketplace.json"
_BIN = _ROOT / "bin" / "redteam-install"
_COMMAND = _ROOT / "commands" / "install.md"

# The generic sub-agents install.py vendors (kept in sync with
# install.py HARNESS_AGENTS — drift here is the bug this guards).
_EXPECTED_AGENTS = {
    "code-security-reviewer",
    "goal-decomposer",
    "implementer",
    "outcome-planner",
    "pr-author",
    "test-author",
    "test-verifier",
}


def test_plugin_manifest_valid():
    """plugin.json is well-formed with the fields Claude Code requires/expects."""
    data = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "redteam"
    assert data["name"] == data["name"].lower().replace(" ", "")  # kebab/lower
    assert data["version"]  # non-empty
    assert data["license"] == "Apache-2.0"  # matches repo LICENSE


def test_marketplace_lists_this_repo_as_the_plugin():
    """marketplace.json self-serves: one plugin sourced at the repo root."""
    data = json.loads(_MARKETPLACE_JSON.read_text(encoding="utf-8"))
    assert data["name"]
    assert data["owner"]["name"]
    plugins = data["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == "redteam"
    assert entry["source"] == "."  # the plugin is this same repo root


def test_marketplace_agents_override_lists_the_vendored_agents():
    """The agents override is a list of individual .md file paths (the marketplace
    schema rejects a bare directory). Each must exist and the set must cover the
    seven skeletons install.py vendors — guards drift between packaging + installer."""
    data = json.loads(_MARKETPLACE_JSON.read_text(encoding="utf-8"))
    agents_paths = data["plugins"][0]["agents"]
    assert isinstance(agents_paths, list) and agents_paths
    resolved = [(_ROOT / a).resolve() for a in agents_paths]
    for r in resolved:
        assert r.is_file() and r.suffix == ".md", f"agents entry not a .md file: {r}"
    stems = {r.stem for r in resolved}
    assert _EXPECTED_AGENTS <= stems, f"missing agents: {_EXPECTED_AGENTS - stems}"


def test_install_wrapper_is_executable_and_self_locating():
    """bin/redteam-install ships +x, self-locates from $0 (not $CLAUDE_PLUGIN_ROOT,
    which isn't guaranteed in command bodies), and execs the bundled installer."""
    assert _BIN.exists()
    if os.name != "nt":  # Windows has no POSIX exec bit; the +x check is POSIX-only
        assert _BIN.stat().st_mode & stat.S_IXUSR, "bin/redteam-install must be executable"
    body = _BIN.read_text(encoding="utf-8")
    assert "BASH_SOURCE" in body  # resolves its own path
    assert ".redteam/scripts/install.py" in body  # targets the real installer
    assert "exec python3" in body


def test_install_command_has_frontmatter():
    """The slash command carries a description so Claude Code surfaces it."""
    body = _COMMAND.read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "description:" in body.split("---", 2)[1]


# The slash commands the plugin ships (beyond install). Drift here — a command
# file added/renamed but not registered, or vice versa — is the bug this guards.
_EXPECTED_COMMANDS = {
    "install",
    "review",
    "config",
    "status",
    "new-task",
}


def test_plugin_and_marketplace_register_the_same_commands():
    """plugin.json and marketplace.json must list the same command set, and it
    must be exactly the command files on disk — no orphans, no dangling refs."""
    plugin_cmds = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["commands"]
    market_cmds = json.loads(_MARKETPLACE_JSON.read_text(encoding="utf-8"))["plugins"][0]["commands"]
    assert plugin_cmds == market_cmds  # the two manifests stay in sync
    stems = {Path(c).stem for c in plugin_cmds}
    assert stems == _EXPECTED_COMMANDS
    on_disk = {p.stem for p in (_ROOT / "commands").glob("*.md")}
    assert on_disk == _EXPECTED_COMMANDS  # every command file is registered


def test_every_command_exists_and_has_frontmatter():
    """Each registered command resolves to a real .md with a description so
    Claude Code surfaces it in the slash-command list."""
    for entry in json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["commands"]:
        path = (_ROOT / entry).resolve()
        assert path.is_file() and path.suffix == ".md", f"missing command file: {entry}"
        body = path.read_text(encoding="utf-8")
        assert body.startswith("---"), f"{entry} missing frontmatter"
        assert "description:" in body.split("---", 2)[1], f"{entry} missing description"
