"""redteam harness configuration.

Every project-specific value the engine needs is externalized here, so the same
engine drives any repo. A project sets these in `.redteam/config.toml`; the
dataclass defaults below are generic placeholders (a vendored install always
seeds a real config.toml, so the defaults are only a last-resort fallback).

The loader FAILS LOUD on unknown keys/sections and bad types: this config is
meant to be the source of truth, so a typo like `verfy_command` must error
rather than silently fall back to a default and mask a project's real verifier.

Uses the stdlib `tomllib` (Python 3.11+) — zero new dependencies, which matters
for a standalone OSS package.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

_CONFIG_RELPATH = (".redteam", "config.toml")
_KNOWN_SECTIONS = ("models", "project")


@dataclass(frozen=True)
class ProjectConfig:
    """Where the target project's code, tests, context and verify command live."""

    name: str = "my-project"
    context_file: str = ".redteam/docs/project-context.md"
    rules_file: str | None = None  # optional extra rules doc injected into agents
    security_checklist: str = ".redteam/docs/security-checklist.md"
    test_conventions_file: str = ".redteam/docs/test-conventions.md"
    source_dirs: tuple[str, ...] = ("src/",)
    test_dir: str = "tests/"
    test_file_glob: str = "test_*.py"  # how the engine recognizes a new test file under test_dir
    verify_command: str = "bash .redteam/scripts/verify.sh"
    branch_prefix: str = "redteam"
    base_branch: str = "main"  # PR base / review diff base


@dataclass(frozen=True)
class ModelsConfig:
    """Role → model. Resolved to adapters by the registry (step 3)."""

    planner: str = "claude-opus-4-7"
    implementer: str = "claude-sonnet-4-6"
    reviewer: str = "codex"
    rescue: str = "codex"


@dataclass(frozen=True)
class RedteamConfig:
    project: ProjectConfig
    models: ModelsConfig


def _build(cls, overrides: dict):
    """Construct a frozen config from a TOML section.

    Unknown keys raise (catch typos). TOML arrays become tuples for the
    tuple-typed fields. Field-value validation happens in `_validate`.
    """
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(overrides) - known
    if unknown:
        section = cls.__name__.removesuffix("Config").lower()
        raise ValueError(f"Unknown {section} config key(s): {sorted(unknown)}. Known keys: {sorted(known)}.")
    kwargs = {k: (tuple(v) if isinstance(v, list) else v) for k, v in overrides.items()}
    return cls(**kwargs)


def _require_nonempty_str(section: str, name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{section}.{name} must be a non-empty string, got {value!r}.")


def _validate(cfg: RedteamConfig) -> None:
    p = cfg.project
    for name in (
        "name",
        "context_file",
        "security_checklist",
        "test_conventions_file",
        "test_dir",
        "test_file_glob",
        "verify_command",
        "branch_prefix",
        "base_branch",
    ):
        _require_nonempty_str("project", name, getattr(p, name))
    if p.rules_file is not None:
        _require_nonempty_str("project", "rules_file", p.rules_file)
    if (
        not isinstance(p.source_dirs, tuple)
        or not p.source_dirs
        or not all(isinstance(d, str) and d for d in p.source_dirs)
    ):
        raise ValueError(f"project.source_dirs must be a non-empty list of non-empty strings, got {p.source_dirs!r}.")
    m = cfg.models
    for name in ("planner", "implementer", "reviewer", "rescue"):
        _require_nonempty_str("models", name, getattr(m, name))


def load_config(repo_root: Path) -> RedteamConfig:
    """Load `.redteam/config.toml` under `repo_root`.

    Missing file → all defaults (generic placeholders). Partial file → only
    the specified keys override; siblings keep their defaults. Unknown
    keys/sections or bad value types raise ValueError.
    """
    path = repo_root.joinpath(*_CONFIG_RELPATH)
    if not path.exists():
        return RedteamConfig(project=ProjectConfig(), models=ModelsConfig())
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown_sections = set(data) - set(_KNOWN_SECTIONS)
    if unknown_sections:
        raise ValueError(
            f"Unknown config section(s): {sorted(unknown_sections)}. Known sections: {list(_KNOWN_SECTIONS)}."
        )
    cfg = RedteamConfig(
        project=_build(ProjectConfig, data.get("project", {})),
        models=_build(ModelsConfig, data.get("models", {})),
    )
    _validate(cfg)
    return cfg
