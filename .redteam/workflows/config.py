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
class ReviewStagesConfig:
    """Round-staged reviewer config: first N rounds use the cheap first-pass
    reviewer; round N+1 and onward escalate to the configured frontier reviewer.

    Only valid as a global subtable (`[models.review_stages]`); tier-level
    staging is explicitly out of scope for v1.
    """

    first_pass_reviewer: str  # a key of adapters._REVIEWER_ADAPTERS
    escalate_after: int  # rounds 1..escalate_after use first-pass; >=1, not bool


@dataclass(frozen=True)
class ReviewCeilingsConfig:
    """Hard ceilings on the review_code loop: max rounds and/or max cumulative
    wall-clock seconds spent inside headless reviewer dispatch.

    Both fields default None (absent = no ceiling). Both must be int >= 1 when
    set (bool values rejected). Presence of the subtable with BOTH absent is a
    config error. Only valid as a global subtable (`[models.review_ceilings]`);
    tier-level ceilings are explicitly out of scope for v1.
    """

    max_review_rounds: int | None = None
    max_wall_clock_sec: int | None = None


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

    planner: str = "claude-opus-5"
    implementer: str = "claude-sonnet-5"
    reviewer: str = "codex"
    rescue: str = "codex"
    # Fallback reviewer when the primary reviewer fails on INFRA (missing CLI,
    # auth, timeout, unparseable) — never on a valid CHANGES_REQUESTED (#37). A
    # provider key (must differ from the worker provider, or its APPROVED won't be
    # trusted) or "manual"/"human" to block for a pasted review. Default "manual"
    # = fail-closed (an infra failure never becomes an automatic approval).
    reviewer_fallback: str = "manual"
    # Optional round-staged reviewer: cheap first-pass for early rounds, frontier
    # escalation on later rounds. Absent (None) = default behavior unchanged.
    # Parsed from the `[models.review_stages]` TOML subtable; validated by
    # _parse_review_stages before _build receives it.
    review_stages: ReviewStagesConfig | None = None
    # Optional hard ceilings on the review_code loop (P5). Absent (None) = no
    # ceilings, today's behavior byte-for-byte. Parsed from the
    # `[models.review_ceilings]` TOML subtable; validated by _parse_review_ceilings.
    review_ceilings: ReviewCeilingsConfig | None = None


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


def _parse_review_ceilings(raw: object) -> ReviewCeilingsConfig:
    """Parse and validate a `[models.review_ceilings]` TOML subtable.

    Fails loud on: unknown keys, subtable present but both keys absent (empty
    subtable), max_review_rounds or max_wall_clock_sec that is a bool / non-int /
    < 1. No adapter dependency (validates int shapes only, unlike _parse_review_stages).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"models.review_ceilings must be a table, got {raw!r}.")
    known = {f.name for f in dataclasses.fields(ReviewCeilingsConfig)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"Unknown models.review_ceilings config key(s): {sorted(unknown)}. Known keys: {sorted(known)}."
        )
    max_rounds = raw.get("max_review_rounds")
    max_wc = raw.get("max_wall_clock_sec")
    if max_rounds is None and max_wc is None:
        raise ValueError(
            "models.review_ceilings subtable is present but both keys are absent; "
            "set at least one of max_review_rounds or max_wall_clock_sec."
        )
    for name, val in (("max_review_rounds", max_rounds), ("max_wall_clock_sec", max_wc)):
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise ValueError(f"models.review_ceilings.{name} must be an int >= 1 (bool values rejected), got {val!r}.")
    return ReviewCeilingsConfig(
        max_review_rounds=max_rounds,
        max_wall_clock_sec=max_wc,
    )


# review_stages and review_ceilings are nested subtable configs, not tier-level
# per-role overrides; exclude them from _KNOWN_ROLES so a [tiers.N].models key
# of either name continues to be rejected by the "unknown role(s)" fail-loud
# path in _parse_tiers (D1 of P3; D1 of P5).
_KNOWN_ROLES = frozenset(f.name for f in dataclasses.fields(ModelsConfig)) - {"review_stages", "review_ceilings"}

# reviewer_fallback is POLICY, not a model name: it must be a known reviewer
# provider or a manual sentinel. Validated loudly (a typo fails at load) in BOTH
# the top-level [models] block and any [tiers.N.models] override (#37).
_ALLOWED_REVIEWER_FALLBACK = ("codex", "claude", "manual", "human")


def _validate_reviewer_fallback(where: str, value: str) -> None:
    if value not in _ALLOWED_REVIEWER_FALLBACK:
        raise ValueError(f"{where} must be one of {list(_ALLOWED_REVIEWER_FALLBACK)}, got {value!r}.")


_MANUAL_SENTINELS = frozenset({"manual", "human"})


def _parse_review_stages(raw: object) -> ReviewStagesConfig:
    """Parse and validate a `[models.review_stages]` TOML subtable.

    Fails loud on: unknown keys, missing required keys, manual/human first-pass
    reviewer (defeats the cost-cutting purpose), first_pass_reviewer not a
    registered adapter key, escalate_after that is a bool / non-int / < 1.

    The adapter-key check uses a lazy import of `adapters._REVIEWER_ADAPTERS` to
    avoid a module-level import cycle (adapters → phase_runners._base → config
    is already a lazy-load chain; adding config → adapters at module level would
    introduce a top-level cycle).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"models.review_stages must be a table, got {raw!r}.")
    known = {f.name for f in dataclasses.fields(ReviewStagesConfig)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown models.review_stages config key(s): {sorted(unknown)}. Known keys: {sorted(known)}.")
    # first_pass_reviewer
    first_pass = raw.get("first_pass_reviewer")
    if first_pass is None:
        raise ValueError("models.review_stages.first_pass_reviewer is required.")
    if not isinstance(first_pass, str):
        raise ValueError(f"models.review_stages.first_pass_reviewer must be a string, got {first_pass!r}.")
    if first_pass in _MANUAL_SENTINELS:
        raise ValueError(
            f"models.review_stages.first_pass_reviewer cannot be 'manual' or 'human' "
            f"(a manual first-pass defeats the cost-cutting purpose); got {first_pass!r}."
        )
    # Lazy import to avoid a top-level import cycle (adapters imports phase_runners._base
    # which lazily imports config; config importing adapters at module level would close
    # the cycle at load time).
    from adapters import _REVIEWER_ADAPTERS  # noqa: PLC0415

    if first_pass not in _REVIEWER_ADAPTERS:
        raise ValueError(
            f"models.review_stages.first_pass_reviewer must be a registered reviewer adapter key, "
            f"got {first_pass!r}. Known: {sorted(_REVIEWER_ADAPTERS)}."
        )
    # escalate_after
    escalate = raw.get("escalate_after")
    if escalate is None:
        raise ValueError("models.review_stages.escalate_after is required.")
    if isinstance(escalate, bool) or not isinstance(escalate, int) or escalate < 1:
        raise ValueError(
            f"models.review_stages.escalate_after must be an int >= 1 (bool values rejected), got {escalate!r}."
        )
    return ReviewStagesConfig(first_pass_reviewer=first_pass, escalate_after=escalate)


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
    # Pre-process [models.review_stages] subtable before _build so _build receives
    # a ReviewStagesConfig instance (or None) rather than a raw dict.  _build's
    # known-key check allows review_stages on ModelsConfig; _parse_review_stages
    # validates the nested keys fail-loud before that point.
    models_raw = dict(data.get("models", {}))
    review_stages_raw = models_raw.pop("review_stages", None)
    if review_stages_raw is not None:
        models_raw["review_stages"] = _parse_review_stages(review_stages_raw)
    # Pre-process [models.review_ceilings] subtable before _build (same pattern
    # as review_stages above): _build's known-key check allows review_ceilings on
    # ModelsConfig; _parse_review_ceilings validates the nested keys fail-loud.
    review_ceilings_raw = models_raw.pop("review_ceilings", None)
    if review_ceilings_raw is not None:
        models_raw["review_ceilings"] = _parse_review_ceilings(review_ceilings_raw)
    cfg = RedteamConfig(
        project=_build(ProjectConfig, data.get("project", {})),
        models=_build(ModelsConfig, models_raw),
        tiers=_parse_tiers(data.get("tiers", {})),
        tier_triggers=triggers,
        default_tier=default_tier,
    )
    _validate(cfg)
    _validate_tiers(cfg)
    return cfg
