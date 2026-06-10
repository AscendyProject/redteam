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
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

_CONFIG_RELPATH = (".redteam", "config.toml")
_KNOWN_SECTIONS = ("models", "project", "tiers", "tier_triggers")


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
    # Bare-tool allowlist for verification commands an LLM-authored outcome.md may
    # propose. The configured verify_command is always exact-argv-trusted; any
    # OTHER command must name one of these tools (or `python -m <tool>`). Default
    # is a Python stack; a JS project sets e.g. ("vitest", "eslint", "tsc"). This
    # is a security boundary — it bounds what the planner can get executed.
    verification_allowlist: tuple[str, ...] = ("pytest", "ruff", "mypy")
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
class TierProfile:
    """Execution posture for one risk tier (roadmap item B / issue #13).

    `phases` overrides the phase order for a task at this tier (None → the
    engine's default order). `models` overrides specific roles for this tier
    (merged over `[models]`), so e.g. a Tier-0 trivial change can use a cheap
    implementer. Empty/absent fields leave the engine default in place.
    """

    phases: tuple[str, ...] | None = None
    models: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RedteamConfig:
    project: ProjectConfig
    models: ModelsConfig
    # Tier-aware routing (opt-in). Empty `tiers` → routing is OFF and the engine
    # behaves exactly as before (one implicit tier = the default pipeline).
    tiers: dict[int, TierProfile] = field(default_factory=dict)
    tier_triggers: dict[str, int] = field(default_factory=dict)  # path-glob → minimum tier
    default_tier: int | None = None  # tier for an unclassified task (safe default)


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
    if (
        not isinstance(p.verification_allowlist, tuple)
        or not p.verification_allowlist
        or not all(isinstance(t, str) and t for t in p.verification_allowlist)
    ):
        raise ValueError(
            f"project.verification_allowlist must be a non-empty list of non-empty strings, "
            f"got {p.verification_allowlist!r}."
        )
    m = cfg.models
    for name in ("planner", "implementer", "reviewer", "rescue"):
        _require_nonempty_str("models", name, getattr(m, name))


_KNOWN_ROLES = frozenset(f.name for f in dataclasses.fields(ModelsConfig))


def _parse_tiers(raw: dict) -> dict[int, TierProfile]:
    """Parse `[tiers.<N>]` tables. Keys must be integers; each profile may set
    `phases` (list of non-empty strings) and `models` (role → non-empty string,
    role ∈ ModelsConfig fields). Fails loud on anything else."""
    tiers: dict[int, TierProfile] = {}
    for key, prof in raw.items():
        try:
            tier = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"[tiers] keys must be integers, got {key!r}.")
        if tier < 0:
            raise ValueError(f"[tiers] keys must be non-negative, got {tier}.")
        if not isinstance(prof, dict):
            raise ValueError(f"[tiers.{tier}] must be a table, got {prof!r}.")
        unknown = set(prof) - {"phases", "models"}
        if unknown:
            raise ValueError(f"[tiers.{tier}] unknown key(s): {sorted(unknown)}. Known: ['models', 'phases'].")
        phases = prof.get("phases")
        if phases is not None:
            if not isinstance(phases, list) or not phases or not all(isinstance(p, str) and p for p in phases):
                raise ValueError(
                    f"[tiers.{tier}].phases must be a non-empty list of non-empty strings, got {phases!r}."
                )
            phases = tuple(phases)
        models = prof.get("models", {})
        if not isinstance(models, dict):
            raise ValueError(f"[tiers.{tier}].models must be a table, got {models!r}.")
        bad_roles = set(models) - _KNOWN_ROLES
        if bad_roles:
            raise ValueError(
                f"[tiers.{tier}].models has unknown role(s): {sorted(bad_roles)}. Known: {sorted(_KNOWN_ROLES)}."
            )
        for role, model in models.items():
            if not isinstance(model, str) or not model:
                raise ValueError(f"[tiers.{tier}].models.{role} must be a non-empty string, got {model!r}.")
        tiers[tier] = TierProfile(phases=phases, models=dict(models))
    return tiers


def _parse_triggers(raw: dict) -> tuple[dict[str, int], int | None]:
    """Parse `[tier_triggers]`: glob → tier int, plus an optional `default`."""
    triggers: dict[str, int] = {}
    default_tier: int | None = None
    for key, val in raw.items():
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"[tier_triggers].{key} must be a non-negative integer tier, got {val!r}.")
        if key == "default":
            default_tier = val
        else:
            if not isinstance(key, str) or not key:
                raise ValueError(f"[tier_triggers] glob keys must be non-empty strings, got {key!r}.")
            triggers[key] = val
    return triggers, default_tier


def _validate_tiers(cfg: RedteamConfig) -> None:
    """Cross-checks once tiers are parsed. Routing is opt-in: no `[tiers]` →
    nothing to validate. When `[tiers]` IS present we fail loud so a
    misconfiguration can never silently under-review a change."""
    if not cfg.tiers:
        # Triggers/default without any tier profile would route nowhere → reject.
        if cfg.tier_triggers or cfg.default_tier is not None:
            raise ValueError("[tier_triggers] is set but [tiers] defines no tier profiles.")
        return
    # A safe default is mandatory once tiers exist, so an unclassified task
    # always resolves to an explicit, operator-chosen posture.
    if cfg.default_tier is None:
        raise ValueError("[tiers] is set but [tier_triggers].default is missing — declare a safe default tier.")
    # Every tier referenced by default/triggers must have a profile.
    referenced = {cfg.default_tier, *cfg.tier_triggers.values()}
    missing = sorted(t for t in referenced if t not in cfg.tiers)
    if missing:
        raise ValueError(f"tiers referenced by [tier_triggers] have no [tiers.<N>] profile: {missing}.")


def resolve_tier(cfg: RedteamConfig, explicit_tier: int | None, affected_paths: list[str] | None) -> int | None:
    """Resolve the binding tier for a task, or None if tier routing is OFF.

    Binding tier = max(explicit declaration, every trigger glob matching an
    affected path, the safe default). Deterministic and monotonic: an explicit
    declaration can only RAISE the tier above what the path triggers demand,
    never lower it. An unclassified task falls back to the safe default.
    """
    if not cfg.tiers:
        return None
    candidates: list[int] = [cfg.default_tier] if cfg.default_tier is not None else []
    for path in affected_paths or []:
        for glob, tier in cfg.tier_triggers.items():
            if fnmatch(path, glob):
                candidates.append(tier)
    if explicit_tier is not None:
        if explicit_tier not in cfg.tiers:
            raise ValueError(f"task declared tier {explicit_tier}, which has no [tiers.{explicit_tier}] profile.")
        candidates.append(explicit_tier)
    binding = max(candidates)
    if binding not in cfg.tiers:
        raise ValueError(f"resolved tier {binding} has no [tiers.{binding}] profile.")
    return binding


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
    triggers, default_tier = _parse_triggers(data.get("tier_triggers", {}))
    cfg = RedteamConfig(
        project=_build(ProjectConfig, data.get("project", {})),
        models=_build(ModelsConfig, data.get("models", {})),
        tiers=_parse_tiers(data.get("tiers", {})),
        tier_triggers=triggers,
        default_tier=default_tier,
    )
    _validate(cfg)
    _validate_tiers(cfg)
    return cfg
