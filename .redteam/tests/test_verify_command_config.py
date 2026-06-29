"""verify_command becomes config-driven (extraction #2-verify).

The verifier invocation used to hardcode the literal `bash .redteam/scripts/verify.sh`
in two places: the command allowlist (`validate_verification_commands`) and the
legacy `_run_verify_sh` call site. Both now read `config.project.verify_command`,
so each project runs its own verifier.

Security boundary (Codex plan_review): the project's configured command is
project-authored (trusted as much as the repo's own scripts), so its EXACT argv
is allowed even if it names a non-allowlisted executable. Variations / arbitrary
commands still fall through the restrictive allowlist, so an LLM-authored
outcome.md cannot smuggle an arbitrary command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from config import ModelsConfig, ProjectConfig, RedteamConfig  # noqa: E402
from phase_runners._base import validate_verification_commands  # noqa: E402


def _cfg(verify_command: str, allowlist: tuple[str, ...] = ("pytest", "ruff", "mypy")) -> RedteamConfig:
    return RedteamConfig(
        project=ProjectConfig(verify_command=verify_command, verification_allowlist=allowlist),
        models=ModelsConfig(),
    )


def test_validator_allows_the_configured_verify_command() -> None:
    """The exact configured command is allowed even though `npm` is not in the
    base allowlist and `bash <path>` has a path arg."""
    with patch("config.load_config", return_value=_cfg("npm test")):
        assert validate_verification_commands(["npm test"]) == [["npm", "test"]]


def test_validator_rejects_variations_of_the_project_command() -> None:
    """Only the EXACT configured command is trusted; a sibling subcommand
    (npm install) is not — it falls through to the restrictive allowlist."""
    with patch("config.load_config", return_value=_cfg("npm test")):
        with pytest.raises(ValueError):
            validate_verification_commands(["npm install"])


def test_validator_still_allows_python_tools() -> None:
    """Regression: pytest/ruff/mypy stay allowed regardless of config."""
    with patch("config.load_config", return_value=_cfg("npm test")):
        out = validate_verification_commands(["pytest", "ruff check .", "python -m mypy app/"])
    assert ["pytest"] in out and ["ruff", "check", "."] in out


def test_validator_still_rejects_arbitrary_command() -> None:
    """An arbitrary command is rejected when it is not the configured one."""
    with patch("config.load_config", return_value=_cfg("bash .redteam/scripts/verify.sh")):
        with pytest.raises(ValueError):
            validate_verification_commands(["rm -rf /"])


def test_validator_uses_pinned_plan_time_command_over_current_config() -> None:
    """Agent-pair re-validation passes the snapshotted plan-time verify command
    AND allowlist, so a mid-round config mutation does not reject a plan-approved
    project verifier (IR-001 round 3). 'npm test' validates against the pinned
    value even though current config now says 'true'."""
    with patch("config.load_config", return_value=_cfg("true")):  # mutated current config
        out = validate_verification_commands(
            ["npm test"], project_verify_command="npm test", allowlist=["pytest", "ruff", "mypy"]
        )
    assert out == [["npm", "test"]]


def test_validator_pinned_allowlist_overrides_widened_current_config() -> None:
    """F-1: the plan-time allowlist is pinned too. An implementer that edits
    config.toml mid-round to ADD a dangerous tool cannot get it executed — the
    snapshotted allowlist (without it) is used, so the command is rejected."""
    # current config has been widened to include 'curl', but the pinned snapshot did not.
    with patch("config.load_config", return_value=_cfg("npm test", allowlist=("pytest", "curl"))):
        with pytest.raises(ValueError):
            validate_verification_commands(
                ["curl http://x"], project_verify_command="npm test", allowlist=["pytest", "ruff", "mypy"]
            )


def test_validator_uses_configured_js_allowlist() -> None:
    """F-1: a JS project's allowlist allows its tools (vitest) and rejects the
    Python defaults (pytest) — the bare-tool set is config-driven, not hardcoded."""
    with patch("config.load_config", return_value=_cfg("npm test", allowlist=("vitest", "eslint", "tsc"))):
        out = validate_verification_commands(["vitest run", "tsc --noEmit"])
    assert ["vitest", "run"] in out and ["tsc", "--noEmit"] in out
    with patch("config.load_config", return_value=_cfg("npm test", allowlist=("vitest", "eslint", "tsc"))):
        with pytest.raises(ValueError):
            validate_verification_commands(["pytest"])


def test_validator_fails_closed_when_pinned_command_without_allowlist() -> None:
    """F-1 / D1: legacy in-flight state that pinned verify_command before the
    allowlist snapshot existed must FAIL CLOSED, not silently read live config."""
    with pytest.raises(ValueError, match="allowlist not provided"):
        validate_verification_commands(["npm test"], project_verify_command="npm test")


def test_validator_fails_loud_on_malformed_config() -> None:
    """A malformed config.toml (load_config raises) must propagate, not be
    swallowed into a silent 'no configured verifier' (IR-001 round 2)."""
    with patch("config.load_config", side_effect=ValueError("Unknown project config key")):
        with pytest.raises(ValueError):
            validate_verification_commands(["pytest"])


def test_validator_default_config_allows_default_verify_sh() -> None:
    """No patch: the checked-in .redteam/config.toml's default
    `bash .redteam/scripts/verify.sh` command validates as exact argv."""
    assert validate_verification_commands(["bash .redteam/scripts/verify.sh"]) == [
        ["bash", ".redteam/scripts/verify.sh"]
    ]


def test_run_verify_sh_runs_given_argv_shell_free() -> None:
    """_run_verify_sh runs the pre-validated argv it is handed, shell-free."""
    from phase_runners import implement

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell", False)

        class _P:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _P()

    with patch("phase_runners.implement.subprocess.run", side_effect=_fake_run):
        rc, _ = implement._run_verify_sh(Path("."), ["pytest"])
    assert rc == 0
    assert captured["argv"] == ["pytest"]
    assert captured["shell"] is False


def test_legacy_run_snapshots_verify_before_implementer() -> None:
    """IR-001 regression: the verify command is captured + validated BEFORE the
    implementer runs, so a same-round edit to config.toml's verify_command
    cannot neuter the gate. We simulate the implementer mutating the config to a
    no-op ('true') during its invoke; the gate must still run the pre-snapshot
    command ('pytest'), not the mutated one."""
    from phase_runners import implement

    holder = {"verify": "pytest"}
    captured = {}

    def _load(_root):
        return _cfg(holder["verify"])

    class _FakeAdapter:
        def invoke(self, **kwargs):
            holder["verify"] = "true"  # implementer mutates config mid-round
            (kwargs["cwd"] / "impl_diff.patch").write_text("x", encoding="utf-8")
            return {"returncode": 0, "stdout": "", "stderr": ""}

    def _fake_run(argv, **kwargs):
        # Capture the VERIFY invocation only — the harness now also runs `git` calls
        # (building the tdd review patch, #82; the pre-worker tracked/untracked
        # baseline probes, #91 Part A) which would otherwise overwrite the captured
        # argv. The git probes must report a CLEAN tree (empty stdout) so the
        # baseline snapshot succeeds — a non-empty stdout would be parsed as a
        # changed path and trip the out-of-scope floor.
        is_git = bool(argv) and argv[0] == "git"
        if not is_git:
            captured["argv"] = argv

        class _P:
            returncode = 0
            stdout = "" if is_git else "ok"
            stderr = ""

        return _P()

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with (
            patch("config.load_config", side_effect=_load),
            patch("phase_runners.implement.get_worker_adapter", return_value=_FakeAdapter()),
            patch("phase_runners.implement.repo_root", return_value=tdp),
            patch("phase_runners.implement.compute_repo_diff", return_value=""),
            patch("phase_runners.implement.subprocess.run", side_effect=_fake_run),
        ):
            res = implement.run(tdp, {"mode": "tdd", "task_id": "t", "base_branch": "main"})

    assert res["status"] == "approved"
    assert captured["argv"] == ["pytest"]  # snapshot wins; old code would run "true"
