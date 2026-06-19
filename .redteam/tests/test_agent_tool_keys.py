"""Subagent tool-restriction frontmatter is correct and pinned (#76).

Claude Code restricts a subagent's tools with the `tools:` frontmatter key.
`allowed-tools:` is the slash-command / settings key and is silently ignored on
a subagent — the agent then inherits the parent's FULL tool set, defeating the
per-agent restriction. This guards against (a) regressing to `allowed-tools:`,
and (b) a tool list drifting out of sync with what each agent actually needs
(e.g. a phase that must Write its output file losing Write).
"""

from __future__ import annotations

from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "agents"

# Each agent's pinned tool set = exactly what its body/runner contract needs.
# Writers of an output artifact need Write: outcome-planner (outcome.md),
# test-author (tests), the reviewers (their *_review.md), pr-author (pr.md).
# Bash is for agents that run a command (tests / scanner / git+gh). Sandbox-enforced
# read-only applies specifically to the headless REVIEWER-adapter path (`codex
# --sandbox read-only` / `claude --permission-mode plan`) — worker phases run
# workspace-write. This frontmatter is the tool restriction for the in-session
# sub-agent path; it is not, by itself, an airtight read-only boundary.
_EXPECTED_TOOLS = {
    "outcome-planner.md": {"Read", "Grep", "Glob", "Write"},
    "test-author.md": {"Read", "Grep", "Write", "Bash"},
    "test-verifier.md": {"Read", "Grep", "Bash", "Write"},
    "implementer.md": {"Read", "Grep", "Edit", "Write", "Bash"},
    "code-security-reviewer.md": {"Read", "Grep", "Bash", "Write"},
    "pr-author.md": {"Read", "Write", "Bash"},
}


def _frontmatter_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path.name}: missing YAML frontmatter"
    end = text.index("\n---", 3)
    return text[3:end].splitlines()


def _tools_value(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("tools:"):
            return line[len("tools:") :].strip()
    return None


def test_no_agent_uses_allowed_tools_key():
    """`allowed-tools:` on a subagent is silently ignored — none may use it."""
    offenders = [
        p.name
        for p in _AGENTS_DIR.glob("*.md")
        if any(line.startswith("allowed-tools:") for line in _frontmatter_lines(p))
    ]
    assert offenders == [], f"subagent(s) use the ignored `allowed-tools:` key: {offenders}"


def test_every_expected_agent_declares_tools_with_pinned_set():
    for name, expected in _EXPECTED_TOOLS.items():
        path = _AGENTS_DIR / name
        assert path.exists(), f"missing agent definition: {name}"
        value = _tools_value(_frontmatter_lines(path))
        assert value is not None, f"{name}: no `tools:` key (restriction would be absent)"
        got = {t.strip() for t in value.split(",") if t.strip()}
        assert got == expected, f"{name}: tools {got} != pinned {expected}"


def test_writer_phases_have_write():
    """Every agent that must produce an output file needs the Write tool — a
    naive allowed-tools→tools rename that left these without Write would break the
    pipeline (e.g. outcome-planner could not create outcome.md)."""
    for name in ("outcome-planner.md", "code-security-reviewer.md", "test-verifier.md"):
        value = _tools_value(_frontmatter_lines(_AGENTS_DIR / name)) or ""
        assert "Write" in value, f"{name} writes an output file but lacks Write"
