"""config_cli — interactive per-role model picker for .redteam/config.toml.

Public surface: run_config(repo, pairing_guard) -> int.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Any

from config import ModelsConfig

_ROLES: tuple[str, ...] = ("planner", "implementer", "reviewer", "rescue")

# Matches exactly the four target role keys with a double-quoted value.
# Groups: (leading-ws)(role)(= separator)("value")(trailing, e.g. inline comment).
# Does NOT match reviewer_fallback: the '=' separator check rejects the '_fallback' suffix.
_ROLE_LINE_RE = re.compile(r'^(\s*)(planner|implementer|reviewer|rescue)(\s*=\s*)"([^"]*)"(.*)')


def _field_defaults() -> dict[str, str]:
    return {f.name: f.default for f in dataclasses.fields(ModelsConfig) if f.name in _ROLES}  # type: ignore[arg-type]


def _parse_models_section(
    lines: list[str],
) -> tuple[bool, dict[str, str], dict[str, str]]:
    """Scan lines for [models].

    Returns (found_section, role_values, extra_model_keys).
    Extra keys (e.g. reviewer_fallback) are preserved byte-for-byte and passed
    through to the guard state so it sees the same shape as in production.
    """
    found = False
    in_models = False
    roles: dict[str, str] = {}
    extras: dict[str, str] = {}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section_name = stripped.split("#")[0].strip()
            in_models = section_name == "[models]"
            if in_models:
                found = True
            continue
        if not in_models:
            continue
        line_body = line.rstrip("\r\n")
        m = _ROLE_LINE_RE.match(line_body)
        if m:
            roles[m.group(2)] = m.group(4)
        else:
            other = re.match(r'^\s*(\w+)\s*=\s*"([^"]*)"', line_body)
            if other:
                extras[other.group(1)] = other.group(2)

    return found, roles, extras


def _rewrite_models_block(lines: list[str], new_values: dict[str, str]) -> list[str]:
    """Return new line list with only the four role values replaced.

    Every other line — including inline comments, blank lines, other keys, and
    all non-[models] sections — is passed through unchanged (same object).
    Line endings (\n, \r\n) are preserved exactly.
    """
    result: list[str] = []
    in_models = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section_name = stripped.split("#")[0].strip()
            in_models = section_name == "[models]"
            result.append(line)
            continue
        if not in_models:
            result.append(line)
            continue
        # Inside [models]: replace role lines, pass everything else through.
        line_body = line.rstrip("\r\n")
        line_end = line[len(line_body) :]  # \n or \r\n or ""
        m = _ROLE_LINE_RE.match(line_body)
        if m and m.group(2) in new_values:
            new_body = f'{m.group(1)}{m.group(2)}{m.group(3)}"{new_values[m.group(2)]}"{m.group(5)}'
            result.append(new_body + line_end)
        else:
            result.append(line)

    return result


def run_config(repo: Path, pairing_guard: Any) -> int:
    """Interactively set the four per-role models in .redteam/config.toml.

    pairing_guard(state: dict) -> str | None — injected by caller; mirrors
    orchestrator._adversarial_pairing_error so the follow-up wiring task can
    pass that function directly with no adapter.

    Returns 0 on success, non-zero on error or guard refusal.
    """
    config_path = repo / ".redteam" / "config.toml"

    try:
        with open(config_path, "r", encoding="utf-8", newline="") as fh:
            content = fh.read()
    except FileNotFoundError:
        print(f"error: {config_path} not found", file=sys.stderr)
        return 1

    lines = content.splitlines(keepends=True)
    found_section, current_roles, extra_keys = _parse_models_section(lines)

    if not found_section:
        print("error: .redteam/config.toml has no [models] section", file=sys.stderr)
        return 1

    missing = [r for r in _ROLES if r not in current_roles]
    if missing:
        print(f"error: [models] is missing role(s): {missing}", file=sys.stderr)
        return 1

    defaults = _field_defaults()
    chosen: dict[str, str] = {}
    for role in _ROLES:
        current = current_roles[role]
        default = defaults[role]
        prompt = f"{role} (current: {current!r}, recommended default: {default!r}): "
        raw = input(prompt)
        chosen[role] = raw.strip() or current

    state: dict[str, Any] = {
        "mode": "agent-pair",
        "models": {**extra_keys, **chosen},
    }
    error = pairing_guard(state)
    if error:
        print(error, file=sys.stderr)
        return 1

    new_lines = _rewrite_models_block(lines, chosen)
    with open(config_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(new_lines))
    return 0
