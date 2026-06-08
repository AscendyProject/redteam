"""redteam config loader tests.

The config module is the externalization seam: project-specific values (paths,
verify command, branch prefix, model choices, context/rules files) live in
`.redteam/config.toml` instead of hardcoded literals, so the same engine drives
any repo. These tests pin the loader contract: generic placeholder defaults, a
valid checked-in config, full override by a foreign-stack config, and fail-loud
on typos. TOML is used (stdlib `tomllib`, zero new deps).
"""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import pytest  # noqa: E402

from config import ModelsConfig, load_config  # noqa: E402


def test_defaults_are_generic_placeholders(tmp_path: Path) -> None:
    """No config.toml → generic placeholder defaults. A vendored install always
    seeds a real config.toml, so defaults are only a last-resort fallback and
    must not carry any one project's fingerprints (no ascendy `app/`/`ascendy`)."""
    cfg = load_config(tmp_path)
    # models = sensible current model defaults
    assert cfg.models.planner == "claude-opus-4-7"
    assert cfg.models.implementer == "claude-sonnet-4-6"
    assert cfg.models.reviewer == "codex"
    assert cfg.models.rescue == "codex"
    # project = generic placeholders
    assert cfg.project.name == "my-project"
    assert cfg.project.source_dirs == ("src/",)
    assert cfg.project.test_dir == "tests/"
    assert cfg.project.branch_prefix == "redteam"
    assert cfg.project.verify_command == "bash .redteam/scripts/verify.sh"
    assert cfg.project.context_file == ".redteam/docs/project-context.md"
    assert cfg.project.security_checklist == ".redteam/docs/security-checklist.md"
    assert cfg.project.test_conventions_file == ".redteam/docs/test-conventions.md"
    assert cfg.project.test_file_glob == "test_*.py"
    assert cfg.project.base_branch == "main"


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    """A foreign-stack config.toml fully overrides (validates extraction goal)."""
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(
        "[project]\n"
        'name = "frontend-app"\n'
        'source_dirs = ["src/", "components/"]\n'
        'test_dir = "spec/"\n'
        'branch_prefix = "task"\n'
        'verify_command = "npm test"\n'
        'context_file = ".redteam/ctx.md"\n'
        'test_conventions_file = ".redteam/spec-conventions.md"\n'
        'test_file_glob = "*.spec.ts"\n'
        'base_branch = "develop"\n'
        "\n"
        "[models]\n"
        'planner = "gpt-5"\n'
        'reviewer = "claude-opus-4-8"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.project.name == "frontend-app"
    assert cfg.project.source_dirs == ("src/", "components/")
    assert cfg.project.test_dir == "spec/"
    assert cfg.project.branch_prefix == "task"
    assert cfg.project.verify_command == "npm test"
    assert cfg.project.context_file == ".redteam/ctx.md"
    assert cfg.project.test_conventions_file == ".redteam/spec-conventions.md"
    assert cfg.project.test_file_glob == "*.spec.ts"
    assert cfg.project.base_branch == "develop"
    assert cfg.models.planner == "gpt-5"
    assert cfg.models.reviewer == "claude-opus-4-8"
    # keys not in the toml keep their defaults
    assert cfg.models.rescue == "codex"


def test_partial_toml_keeps_other_defaults(tmp_path: Path) -> None:
    """Specifying one key must not wipe sibling defaults."""
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[models]\nreviewer = "claude-opus-4-8"\n')
    cfg = load_config(tmp_path)
    assert cfg.models.reviewer == "claude-opus-4-8"
    assert cfg.models.planner == "claude-opus-4-7"  # default kept
    assert cfg.project.source_dirs == ("src/",)  # default kept


def test_frozen_config_is_immutable(tmp_path: Path) -> None:
    """Config is read-only once loaded (frozen dataclasses)."""
    import dataclasses

    cfg = load_config(tmp_path)
    for obj in (cfg, cfg.project, cfg.models):
        assert dataclasses.is_dataclass(obj)
        try:
            object.__setattr__  # sanity
            setattr(obj, "branch_prefix", "x")
            raise AssertionError("config should be frozen")
        except dataclasses.FrozenInstanceError:
            pass
        except AttributeError:
            pass  # field not on this object


def test_checked_in_config_toml_loads_and_validates() -> None:
    """The repo's own .redteam/config.toml must load and validate (it describes
    this repo — redteam dogfoods its own harness). This guards the shipped
    config against drift/typos that would fail the loader."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root)
    assert cfg.project.name == "redteam"
    assert cfg.project.branch_prefix == "redteam"
    # source/test dirs point at the harness's own code, not a placeholder
    assert cfg.project.source_dirs == (".redteam/workflows/",)
    assert cfg.project.test_dir == ".redteam/tests/"
    assert cfg.models == ModelsConfig()


def test_seed_template_fails_loud_until_configured(tmp_path: Path) -> None:
    """#7.5 F-B: the shipped seed config.toml must NOT silently run with a wrong
    stack's defaults. Its stack-specific fields ship empty, so loading it as-is
    raises — forcing the operator to configure for their stack."""
    repo_root = Path(__file__).resolve().parents[2]
    template = (repo_root / ".redteam" / "templates" / "config.toml").read_text(encoding="utf-8")
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(template, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(tmp_path)


@pytest.mark.parametrize("example", ["ascendy-like", "nuxt-like"])
def test_example_config_loads_and_validates(tmp_path: Path, example: str) -> None:
    """Shipped examples must be valid, filled-in configs — the counterpart to the
    deliberately-empty seed template (#7.5 F-C). Guards them from drift."""
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "examples" / example / ".redteam" / "config.toml"
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.project.name  # non-empty, validated
    assert cfg.project.source_dirs  # non-empty
    assert cfg.project.verification_allowlist  # non-empty


def test_unknown_key_raises(tmp_path: Path) -> None:
    """A typo'd key must error, not silently keep the default verifier."""
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nverfy_command = "npm test"\n')
    with pytest.raises(ValueError, match="Unknown project config key"):
        load_config(tmp_path)


def test_unknown_section_raises(tmp_path: Path) -> None:
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text("[reviewz]\nx = 1\n")
    with pytest.raises(ValueError, match="Unknown config section"):
        load_config(tmp_path)


def test_source_dirs_as_string_raises(tmp_path: Path) -> None:
    """A bare string (not a list) would char-iterate downstream — reject it."""
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[project]\nsource_dirs = "src/"\n')
    with pytest.raises(ValueError, match="source_dirs must be a non-empty list"):
        load_config(tmp_path)


def test_empty_source_dirs_raises(tmp_path: Path) -> None:
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text("[project]\nsource_dirs = []\n")
    with pytest.raises(ValueError, match="source_dirs must be a non-empty list"):
        load_config(tmp_path)


def test_empty_string_field_raises(tmp_path: Path) -> None:
    (tmp_path / ".redteam").mkdir()
    (tmp_path / ".redteam" / "config.toml").write_text('[models]\nreviewer = ""\n')
    with pytest.raises(ValueError, match="models.reviewer must be a non-empty string"):
        load_config(tmp_path)
