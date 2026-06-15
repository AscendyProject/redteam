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
import re
import tomllib
from dataclasses import dataclass, field
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
    # Fallback reviewer when the primary reviewer fails on INFRA (missing CLI,
    # auth, timeout, unparseable) — never on a valid CHANGES_REQUESTED (#37). A
    # provider key (must differ from the worker provider, or its APPROVED won't be
    # trusted) or "manual"/"human" to block for a pasted review. Default "manual"
    # = fail-closed (an infra failure never becomes an automatic approval).
    reviewer_fallback: str = "manual"


GATE_NAMES = ("outcome", "pr", "rescue")  # the human gates a tier may opt into


@dataclass(frozen=True)
class TierProfile:
    """Execution posture for one risk tier (roadmap item B / issue #13).

    Declarative toggles, not a raw phase list — the engine builds the phase order
    from these over a fixed canonical pipeline, so the order is always coherent
    (no way to compose an order that skips the review/PR tail unsafely):

    - `review`: include the adversarial pair (plan_review + review_code + the
      rescue escalation). False → a single-agent path (plan → implement → PR).
    - `gates`: which HUMAN gates to insert — subset of `outcome` / `pr` /
      `rescue`. The thesis is to scale human intervention to risk, so the lean
      default is no gates (the adversarial pair + verify are the trust); a
      high-risk tier opts gates back in. (`rescue` requires `review`.)
    - `models`: per-role model overrides for this tier (merged over `[models]`),
      e.g. a cheap implementer for trivial work.
    """

    review: bool = True
    gates: tuple[str, ...] = ()
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
    _validate_reviewer_fallback("models.reviewer_fallback", m.reviewer_fallback)


_KNOWN_ROLES = frozenset(f.name for f in dataclasses.fields(ModelsConfig))

# reviewer_fallback is POLICY, not a model name: it must be a known reviewer
# provider or a manual sentinel. Validated loudly (a typo fails at load) in BOTH
# the top-level [models] block and any [tiers.N.models] override (#37).
_ALLOWED_REVIEWER_FALLBACK = ("codex", "claude", "manual", "human")


def _validate_reviewer_fallback(where: str, value: str) -> None:
    if value not in _ALLOWED_REVIEWER_FALLBACK:
        raise ValueError(f"{where} must be one of {list(_ALLOWED_REVIEWER_FALLBACK)}, got {value!r}.")


def _parse_tiers(raw: dict) -> dict[int, TierProfile]:
    """Parse `[tiers.<N>]` tables (keys must be integers). Each profile may set
    `review` (bool), `gates` (subset of GATE_NAMES), and `models` (role →
    non-empty string, role ∈ ModelsConfig fields). Fails loud on anything else."""
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
        unknown = set(prof) - {"review", "gates", "models"}
        if unknown:
            raise ValueError(f"[tiers.{tier}] unknown key(s): {sorted(unknown)}. Known: ['gates', 'models', 'review'].")
        review = prof.get("review", True)
        if not isinstance(review, bool):
            raise ValueError(f"[tiers.{tier}].review must be a boolean, got {review!r}.")
        gates = prof.get("gates", [])
        if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
            raise ValueError(f"[tiers.{tier}].gates must be a list of strings, got {gates!r}.")
        bad_gates = set(gates) - set(GATE_NAMES)
        if bad_gates:
            raise ValueError(
                f"[tiers.{tier}].gates has unknown gate(s): {sorted(bad_gates)}. Known: {list(GATE_NAMES)}."
            )
        if "rescue" in gates and not review:
            raise ValueError(f"[tiers.{tier}] has gate 'rescue' but review=false — rescue only happens with review.")
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
            # reviewer_fallback is policy, not a model name — same loud validation
            # as the top-level block, so a tier-override typo fails at load too (#37).
            if role == "reviewer_fallback":
                _validate_reviewer_fallback(f"[tiers.{tier}].models.reviewer_fallback", model)
        tiers[tier] = TierProfile(review=review, gates=tuple(gates), models=dict(models))
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


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a git-pathspec-style glob to a regex with RECURSIVE `**`.

    Unlike stdlib `fnmatch` (where `*` already spans `/` and `**` is meaningless),
    here path separators are meaningful: `*` matches within a single segment
    (no `/`), `**` matches across segments (including none), and `**/` also
    matches zero leading directories so `**/auth/**` matches a top-level `auth/`.
    `?` matches one non-`/` char. This is the matcher operators expect from
    .gitignore-style patterns, so a security trigger like `"**/auth/**"` can't
    silently under-classify a top-level path.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                # `**` — collapse an optional following slash so `**/x` matches `x`.
                i += 2
                if i < n and glob[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("(?s:" + "".join(out) + ")\\Z")


def _path_matches(path: str, glob: str) -> bool:
    return _glob_to_regex(glob).match(path) is not None


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
            if _path_matches(path, glob):
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
