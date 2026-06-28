"""Agent genericization (extraction #4).

The 6 `.claude/agents/*.md` bodies must be project-agnostic (no ascendy/Python
stack literals baked in), and the phase runners must inject the configured
project doc paths / dirs into the worker/reviewer prompts so a foreign-stack
project (e.g. a JS repo with `src/` + `spec/` + `npm test`) drives the same
harness without editing the agent prompts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

from config import ModelsConfig, ProjectConfig, RedteamConfig  # noqa: E402

_AGENTS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "agents"
_AGENT_FILES = [
    "outcome-planner.md",
    "test-author.md",
    "test-verifier.md",
    "implementer.md",
    "code-security-reviewer.md",
    "pr-author.md",
    "goal-decomposer.md",
]

# Tokens that would mean an ascendy/Python literal leaked into a (supposedly
# generic) agent body. The default `.redteam/docs/...` install paths are allowed
# (framed as defaults), so they are NOT in this list.
_FORBIDDEN_IN_AGENTS = [
    "ascendy/<task-id>",
    "bash .redteam/scripts/verify.sh",
    "mypy app/",
    "tests/api/test_",
    "--base main",
    "main...HEAD",
    "Ascendy",
    "BrokerProtocol",
]


def _foreign() -> RedteamConfig:
    return RedteamConfig(
        project=ProjectConfig(
            name="frontend-app",
            context_file=".rt/ctx.md",
            security_checklist=".rt/sec.md",
            test_conventions_file=".rt/testconv.md",
            source_dirs=("src/", "components/"),
            test_dir="spec/",
            test_file_glob="*.spec.ts",
            verify_command="npm test",
            branch_prefix="task",
            base_branch="develop",
        ),
        models=ModelsConfig(),
    )


def test_agent_bodies_have_no_ascendy_or_stack_literals() -> None:
    for name in _AGENT_FILES:
        text = (_AGENTS_DIR / name).read_text(encoding="utf-8")
        for token in _FORBIDDEN_IN_AGENTS:
            assert token not in text, f"{name} still contains ascendy/stack literal {token!r}"


# The markdown templates that GUIDE an agent's fill (vendored into every consumer)
# must be stack-neutral too — a Python-flavored example here biases new projects.
# (config.toml's template is excluded: it intentionally shows both Python and JS
# allowlist examples in comments.)
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
_FILL_TEMPLATES = ["outcome.template.md", "pr.template.md"]
_FORBIDDEN_IN_TEMPLATES = ["pytest", "tests/api", ".py", "mypy", "Ascendy"]


def test_fill_templates_have_no_stack_literals() -> None:
    for name in _FILL_TEMPLATES:
        text = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for token in _FORBIDDEN_IN_TEMPLATES:
            assert token not in text, f"{name} still contains a stack literal {token!r}"


class _Recorder:
    def __init__(self) -> None:
        self.prompt = ""

    def invoke(self, **kwargs):
        self.prompt = kwargs["prompt"]
        return {"returncode": 0, "stdout": "", "stderr": ""}

    def review(self, **kwargs):  # reviewer-adapter shape
        self.prompt = kwargs["prompt"]
        return {"decision": "APPROVED", "raw": "REVIEW_DECISION: APPROVED", "parse_status": "ok"}


def _capture(runner_mod, state, tmp_path, reviewer=False):
    rec = _Recorder()
    getter = "get_reviewer_adapter" if reviewer else "get_worker_adapter"
    with (
        patch("config.load_config", return_value=_foreign()),
        patch(f"phase_runners.{runner_mod.__name__.split('.')[-1]}.{getter}", return_value=rec),
        patch(f"phase_runners.{runner_mod.__name__.split('.')[-1]}.repo_root", return_value=tmp_path),
        patch(f"phase_runners.{runner_mod.__name__.split('.')[-1]}.compute_repo_diff", return_value=""),
    ):
        runner_mod.run(tmp_path, state)
    return rec.prompt


def test_plan_outcome_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import plan_outcome

    prompt = _capture(plan_outcome, {}, tmp_path)
    assert ".rt/ctx.md" in prompt and "src/" in prompt and "spec/" in prompt and "npm test" in prompt
    assert "*.spec.ts" in prompt  # configured test-file pattern, not test_<feature>
    assert "app/" not in prompt and ".redteam/docs/project-context.md" not in prompt


def test_write_test_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import write_test

    # write_test now touches git (untracked snapshot + commit) around the worker
    # invoke; stub that layer so this test isolates the PROMPT the agent gets.
    rec = _Recorder()
    with (
        patch("config.load_config", return_value=_foreign()),
        patch("phase_runners.write_test.get_worker_adapter", return_value=rec),
        patch("phase_runners.write_test.repo_root", return_value=tmp_path),
        patch("phase_runners.write_test.untracked_files", return_value=set()),
        patch("phase_runners.write_test._committed_test_files", return_value=[]),
        patch("phase_runners.write_test.commit_paths", return_value=False),
        patch("phase_runners.write_test.compute_branch_diff", return_value=""),
    ):
        write_test.run(tmp_path, {"base_branch": "develop"})
    prompt = rec.prompt
    assert ".rt/testconv.md" in prompt and "spec/" in prompt
    assert "tests/api/test_" not in prompt and ".redteam/docs/test-conventions.md" not in prompt


def test_implement_agent_pair_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import implement

    # _run_agent_pair now touches git (untracked snapshot + commit) around the
    # worker invoke; stub that layer so this test isolates the PROMPT the agent gets.
    rec = _Recorder()
    with (
        patch("config.load_config", return_value=_foreign()),
        patch("phase_runners.implement.get_worker_adapter", return_value=rec),
        patch("phase_runners.implement.repo_root", return_value=tmp_path),
        patch("phase_runners.implement.compute_repo_diff", return_value=""),
        patch("phase_runners.implement.untracked_files", return_value=set()),
        patch("phase_runners.implement._commit_worker_diff", lambda *a, **k: None),
        patch("phase_runners.implement._uncommitted_scope_files", return_value=[]),
        patch("phase_runners.implement._write_current_diff", return_value=("", "sha")),
        patch("phase_runners.implement._run_verification_commands", return_value=(0, "ok")),
    ):
        implement.run(tmp_path, {"mode": "agent-pair", "base_branch": "develop"})
    prompt = rec.prompt
    assert ".rt/ctx.md" in prompt and "src/" in prompt and "spec/" in prompt
    assert "app/" not in prompt


def test_create_pr_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import create_pr

    rec = _Recorder()
    # create_pr imports load_config at module top, so patch it on the runner module.
    with (
        patch("phase_runners.create_pr.load_config", return_value=_foreign()),
        patch("phase_runners.create_pr.get_worker_adapter", return_value=rec),
        patch("phase_runners.create_pr.repo_root", return_value=tmp_path),
        patch("phase_runners.create_pr.compute_repo_diff", return_value=""),
        # The PR-auth preflight (#51) runs before the worker; stub it to pass so this
        # test exercises the prompt the agent receives (its actual subject).
        patch("phase_runners.create_pr._preflight_pr_auth", return_value=None),
    ):
        create_pr.run(tmp_path, {"base_branch": "develop"})
    prompt = rec.prompt
    # The pr-author prompt is diff-based (reads impl_diff.patch) and mode/tier-neutral (#73),
    # so it no longer injects source/test dirs — but it must still inject the configured
    # branch_prefix and base branch (used by the git/gh commands), not redteam's own.
    assert "task/" in prompt  # branch_prefix=task
    assert "develop" in prompt  # configured base branch, not hardcoded main
    assert "under `tests/`" not in prompt and "app/ changes" not in prompt and "against main" not in prompt


def test_review_code_tdd_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import review_code

    # TDD (non-agent-pair) branch builds the worker prompt with the checklist path.
    prompt = _capture(review_code, {}, tmp_path)
    assert ".rt/sec.md" in prompt and ".rt/ctx.md" in prompt
    assert ".redteam/docs/security-checklist.md" not in prompt


def test_headless_code_review_prompt_injects_foreign_config(tmp_path) -> None:
    from phase_runners import review_code

    with patch("config.load_config", return_value=_foreign()):
        prompt = review_code._code_review_prompt(tmp_path, "develop")
    assert ".rt/sec.md" in prompt and ".rt/ctx.md" in prompt
    assert "develop...HEAD" in prompt and "main...HEAD" not in prompt  # configured base branch
    # IR-004 contract preserved: still read-only / stdout-only.
    assert "stdout only" in prompt and "DO NOT write" in prompt
