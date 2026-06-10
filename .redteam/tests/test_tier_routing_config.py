"""Tier-aware routing — config schema + tier resolution (roadmap item B, issue #13).

Security boundary: the tier decides how much review a change gets, so the
config is opt-in, fails loud on any misconfiguration, and resolution is
deterministic + monotonic (an explicit declaration can RAISE the tier above
what the path triggers demand, never lower it; an unclassified task falls to a
mandatory safe default).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from config import load_config, resolve_tier  # noqa: E402


def _write(tmp_path: Path, body: str):
    (tmp_path / ".redteam").mkdir(exist_ok=True)
    (tmp_path / ".redteam" / "config.toml").write_text(body, encoding="utf-8")
    return load_config(tmp_path)


_VALID = """
[tiers.0]
phases = ["implement"]
models = { implementer = "claude-haiku-4-5" }

[tiers.2]
phases = ["plan_outcome", "plan_review", "implement", "review_code", "create_pr"]

[tiers.4]
phases = ["plan_outcome", "plan_review", "implement", "review_code", "create_pr"]
models = { reviewer = "codex" }

[tier_triggers]
"**/auth/**" = 4
"**/*.md" = 0
default = 2
"""


# --- backward compatibility (routing OFF) ---


def test_no_tiers_section_means_routing_off(tmp_path):
    cfg = _write(
        tmp_path,
        '[project]\nname = "p"\nsource_dirs = ["src/"]\ntest_file_glob = "*.py"\nverification_allowlist = ["pytest"]\n',
    )
    assert cfg.tiers == {}
    assert resolve_tier(cfg, explicit_tier=None, affected_paths=["app/auth/x.py"]) is None
    assert resolve_tier(cfg, explicit_tier=3, affected_paths=None) is None


# --- fail-loud validation (a misconfig must never silently under-review) ---


def test_tiers_without_default_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="default"):
        _write(tmp_path, '[tiers.2]\nphases = ["implement"]\n')


def test_triggers_without_tiers_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="no tier profiles"):
        _write(tmp_path, '[tier_triggers]\n"**/*.py" = 2\ndefault = 2\n')


def test_trigger_references_undefined_tier_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="no \\[tiers"):
        _write(tmp_path, '[tiers.2]\nphases = ["implement"]\n[tier_triggers]\n"**/auth/**" = 4\ndefault = 2\n')


def test_non_integer_tier_key_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="integers"):
        _write(tmp_path, '[tiers.trivial]\nphases = ["implement"]\n[tier_triggers]\ndefault = 0\n')


def test_unknown_role_in_tier_models_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="unknown role"):
        _write(tmp_path, '[tiers.0]\nmodels = { coder = "x" }\n[tier_triggers]\ndefault = 0\n')


def test_bad_phases_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="phases"):
        _write(tmp_path, "[tiers.0]\nphases = []\n[tier_triggers]\ndefault = 0\n")


# --- parsing of a valid config ---


def test_valid_config_parses(tmp_path):
    cfg = _write(tmp_path, _VALID)
    assert set(cfg.tiers) == {0, 2, 4}
    assert cfg.default_tier == 2
    assert cfg.tiers[0].phases == ("implement",)
    assert cfg.tiers[0].models == {"implementer": "claude-haiku-4-5"}
    assert cfg.tiers[2].models == {}
    assert cfg.tier_triggers == {"**/auth/**": 4, "**/*.md": 0}


# --- resolution: deterministic + monotonic + safe default ---


def test_unclassified_falls_to_default(tmp_path):
    cfg = _write(tmp_path, _VALID)
    assert resolve_tier(cfg, explicit_tier=None, affected_paths=None) == 2
    assert resolve_tier(cfg, explicit_tier=None, affected_paths=["src/util.py"]) == 2  # no trigger match → default


def test_trigger_sets_a_floor(tmp_path):
    cfg = _write(tmp_path, _VALID)
    assert resolve_tier(cfg, explicit_tier=None, affected_paths=["app/auth/login.py"]) == 4


def test_explicit_can_raise_but_not_lower_below_trigger(tmp_path):
    cfg = _write(tmp_path, _VALID)
    # explicit lower than the trigger floor → trigger wins (can't lower)
    assert resolve_tier(cfg, explicit_tier=0, affected_paths=["app/auth/login.py"]) == 4
    # explicit higher than everything → explicit wins (can raise)
    assert resolve_tier(cfg, explicit_tier=4, affected_paths=["README.md"]) == 4
    # only a .md change, no explicit → md trigger (0) vs default (2) → max = 2
    assert resolve_tier(cfg, explicit_tier=None, affected_paths=["README.md"]) == 2


def test_explicit_tier_without_profile_fails_loud(tmp_path):
    cfg = _write(tmp_path, _VALID)
    with pytest.raises(ValueError, match="no \\[tiers.3"):
        resolve_tier(cfg, explicit_tier=3, affected_paths=None)
