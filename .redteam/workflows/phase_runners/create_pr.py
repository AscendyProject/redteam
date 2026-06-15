"""Phase 7 — invoke the pr-author. Validates that a draft PR URL was produced."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from adapters import get_worker_adapter

from ._base import (
    PhaseResult,
    compute_repo_diff,
    read_text_if_exists,
    repo_root,
)
from config import load_config


AGENT_NAME = "pr-author"


def _pr_branch(state: dict[str, Any], task_id: str, branch_prefix: str) -> str:
    """Branch the pr-author pushes to. process_task normally saved
    state["branch"] (config-driven, step 2); the prefix-based fallback covers a
    legacy/partial state and is also config-driven (no hardcoded prefix)."""
    return state.get("branch") or f"{branch_prefix}/{task_id}"


def _remote_host(url: str) -> str | None:
    """Host of a git remote URL, for both https and ssh/scp forms.

    https://host/owner/repo(.git), ssh://git@host:port/owner/repo, and the
    scp-like git@host:owner/repo.git all resolve to `host` (e.g. an enterprise
    `github.sec.samsung.net`). Returns None if no host can be parsed.
    """
    url = (url or "").strip()
    if not url:
        return None
    scp = re.match(r"^[\w.+-]+@([^:/]+):", url)  # git@host:owner/repo.git
    if scp:
        return scp.group(1)
    return urlparse(url).hostname


# Short, bounded timeouts: the whole point of the preflight is to fail closed
# FAST, so it must never hang itself (IR-001) — a stuck `gh`/network must not
# out-wait the 900s worker timeout it is meant to pre-empt.
_GIT_REMOTE_TIMEOUT_SEC = 10
_GH_AUTH_TIMEOUT_SEC = 30


def _preflight_pr_auth(cwd: Path) -> str | None:
    """Return an actionable error if a draft PR cannot be created here, else None.

    create_pr shells the pr-author — a headless `claude --print` worker — which runs
    `gh pr create`. If `gh` is missing or not authenticated for the PR target host
    (e.g. an enterprise host while gh is only logged into github.com), that command
    fails, and the headless agent has no way to ask the operator: it stalls until
    the worker's 900s timeout (#51). Fail closed HERE, before invoking the agent,
    with the remedy. Each subprocess is itself bounded by a short timeout so the
    guard cannot become the new hang, and a timeout fails closed (IR-001). The
    PUSH url is checked (not just the fetch url) so split fetch/push remotes or an
    `origin.pushurl` cannot auth the wrong host (IR-002).
    """

    def _run(argv: list[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
        )

    try:
        remote = _run(["git", "remote", "get-url", "--push", "origin"], _GIT_REMOTE_TIMEOUT_SEC)
    except FileNotFoundError:
        return "git not found on PATH — cannot determine the PR remote for create_pr."
    except subprocess.TimeoutExpired:
        return (
            f"`git remote get-url --push origin` timed out after {_GIT_REMOTE_TIMEOUT_SEC}s — "
            "cannot resolve the PR remote. Re-run create_pr."
        )
    if remote.returncode != 0:
        # Do NOT echo git stderr — it can carry a credentialed remote URL / helper
        # output; truncation is not sanitization (#51 review PR-001, IR-002).
        return (
            "could not resolve the `origin` remote for PR creation "
            f"(`git remote get-url --push origin` failed, exit {remote.returncode}). "
            "Add an `origin` remote, then re-run create_pr."
        )
    host = _remote_host(remote.stdout or "")
    if not host:
        # Do NOT echo the raw push URL — it can embed credentials
        # (https://user:token@host/...). Report only that the host is unparseable.
        return (
            "could not parse a host from the origin push URL — cannot verify PR auth. "
            "Fix the remote, then re-run create_pr."
        )
    try:
        auth = _run(["gh", "auth", "status", "--hostname", host], _GH_AUTH_TIMEOUT_SEC)
    except FileNotFoundError:
        return (
            f"`gh` (GitHub CLI) not found on PATH — cannot create the PR on {host}. Install gh "
            f"and run `gh auth login --hostname {host}`, then re-run create_pr."
        )
    except subprocess.TimeoutExpired:
        return (
            f"`gh auth status --hostname {host}` timed out after {_GH_AUTH_TIMEOUT_SEC}s "
            "(network / gh issue) — failing closed rather than letting create_pr hang. "
            f"Check connectivity and `gh auth login --hostname {host}`, then re-run create_pr."
        )
    if auth.returncode != 0:
        return (
            f"`gh` is not authenticated for host {host}, so `gh pr create` would fail and the "
            f"headless pr-author cannot prompt for input. Run `gh auth login --hostname {host}`, "
            "then re-run create_pr."
        )
    return None


def run(task_dir: Path, state: dict[str, Any]) -> PhaseResult:
    task_id = task_dir.name
    rr = repo_root()

    # Fail closed BEFORE invoking the headless pr-author if the PR can't be created
    # (gh missing / not authenticated for the remote host). Otherwise the agent
    # would stall on AskUserQuestion until the 900s worker timeout (#51).
    auth_error = _preflight_pr_auth(rr)
    if auth_error:
        return PhaseResult(status="error", feedback=auth_error, log=auth_error, diff=compute_repo_diff(cwd=rr))

    cfg = load_config(rr)
    branch = _pr_branch(state, task_id, cfg.project.branch_prefix)
    proj = cfg.project

    prompt = (
        f"Create the draft PR for task: {task_id}\n"
        f"Inputs: every artifact under {task_dir} (outcome.md, test_review.md, "
        "impl_diff.patch, code_review.md, input.md, state.json) PLUS the new test "
        f"file the test-author created at the canonical path under `{proj.test_dir}` "
        f"(declared in outcome.md's Affected files) and the implementer's changes under "
        f"{', '.join(proj.source_dirs)}.\n"
        f"Push to branch `{branch}` against base branch `{proj.base_branch}` (use "
        f"`gh pr create --base {proj.base_branch}`), write {task_dir}/pr.md, and save the "
        f"PR URL to {task_dir}/pr_url.txt.\n"
        "ALWAYS pass `--draft`. Never `git push --force`. Follow your agent definition "
        "exactly."
    )

    result = get_worker_adapter(state).invoke(role="planner", agent=AGENT_NAME, prompt=prompt, cwd=rr)
    diff = compute_repo_diff(cwd=rr)

    pr_url_text = read_text_if_exists(task_dir / "pr_url.txt")
    pr_md_text = read_text_if_exists(task_dir / "pr.md")

    if (
        result["returncode"] == 0
        and pr_url_text is not None
        and pr_url_text.strip().startswith("https://")
        and pr_md_text is not None
    ):
        # Persist the PR URL into state for downstream visibility.
        state["pr_url"] = pr_url_text.strip()
        state["branch"] = branch
        return PhaseResult(
            status="approved",
            feedback="",
            log=result["stdout"] + f"\nPR URL: {pr_url_text.strip()}",
            diff=diff,
        )

    feedback_lines = [
        "pr-author phase did not produce the expected outputs.",
        f"returncode={result['returncode']}",
        f"pr.md exists: {pr_md_text is not None}",
        f"pr_url.txt content: {pr_url_text!r}",
        f"stderr (truncated): {result['stderr'][:2000]}",
    ]
    feedback = "\n".join(feedback_lines)
    return PhaseResult(status="error", feedback=feedback, log=feedback, diff=diff)
