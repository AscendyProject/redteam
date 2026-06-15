"""create_pr PR-auth preflight (#51).

Before invoking the headless pr-author, create_pr verifies `gh` can create the PR
(remote host resolvable + gh authenticated for it). If not, it fails closed with
an actionable error INSTEAD of letting the headless agent stall on AskUserQuestion
until the 900s worker timeout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_create_pr():
    workflows_path = Path(__file__).resolve().parents[1] / "workflows"
    if str(workflows_path) not in sys.path:
        sys.path.insert(0, str(workflows_path))
    spec = importlib.util.find_spec("phase_runners.create_pr")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- host parsing ----


def test_remote_host_parses_https_and_enterprise_ssh():
    create_pr = _load_create_pr()
    assert create_pr._remote_host("https://github.sec.samsung.net/bdp/x.git") == "github.sec.samsung.net"
    assert create_pr._remote_host("git@github.sec.samsung.net:bdp/x.git") == "github.sec.samsung.net"
    assert create_pr._remote_host("ssh://git@github.sec.samsung.net:22/bdp/x.git") == "github.sec.samsung.net"
    # non-standard HTTPS port → bare host (gh --hostname wants no port)
    assert create_pr._remote_host("https://github.sec.samsung.net:8443/bdp/x.git") == "github.sec.samsung.net"
    assert create_pr._remote_host("") is None
    assert create_pr._remote_host("not a url") is None


# ---- preflight in run() ----


def _patch_git_remote(create_pr, monkeypatch, *, remote_url, remote_rc=0, gh_rc=0, gh_missing=False, gh_timeout=False):
    """Stub subprocess.run for the preflight: `git remote get-url --push origin` then
    `gh auth status --hostname <host>`. Anything else (compute_repo_diff's git)
    returns empty success."""
    import subprocess as _sp

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(returncode=remote_rc, stdout=remote_url, stderr="boom" if remote_rc else "")
        if argv[:2] == ["gh", "auth"]:
            if gh_missing:
                raise FileNotFoundError("gh")
            if gh_timeout:
                raise _sp.TimeoutExpired(argv, kwargs.get("timeout", 30))
            return SimpleNamespace(returncode=gh_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(create_pr.subprocess, "run", fake_run)
    monkeypatch.setattr(create_pr, "compute_repo_diff", lambda cwd: "")
    monkeypatch.setattr(create_pr, "repo_root", lambda: Path("/repo"))


def _worker_spy():
    calls = {"invoked": False}

    def _get(state):
        def invoke(**kwargs):
            calls["invoked"] = True
            return {"returncode": 0, "stdout": "", "stderr": ""}

        return SimpleNamespace(invoke=invoke)

    return calls, _get


def test_unauthenticated_host_fails_closed_without_invoking_worker(monkeypatch, tmp_path):
    create_pr = _load_create_pr()
    _patch_git_remote(create_pr, monkeypatch, remote_url="https://github.sec.samsung.net/bdp/x.git", gh_rc=1)
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert result["status"] == "error"
    assert "github.sec.samsung.net" in result["feedback"]
    assert "gh auth login --hostname github.sec.samsung.net" in result["feedback"]
    assert calls["invoked"] is False  # never reached the headless agent


def test_gh_missing_fails_actionable(monkeypatch, tmp_path):
    create_pr = _load_create_pr()
    _patch_git_remote(create_pr, monkeypatch, remote_url="https://github.com/o/r.git", gh_missing=True)
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert result["status"] == "error"
    assert "gh" in result["feedback"] and "Install" in result["feedback"]
    assert calls["invoked"] is False


def test_unparseable_remote_fails_actionable(monkeypatch, tmp_path):
    create_pr = _load_create_pr()
    _patch_git_remote(create_pr, monkeypatch, remote_url="", remote_rc=1)
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert result["status"] == "error"
    assert "origin" in result["feedback"]
    assert calls["invoked"] is False


def test_preflight_checks_push_host_not_fetch_host(monkeypatch, tmp_path):
    """IR-003: pin that the preflight authenticates the PUSH target host. The fetch
    URL and push URL resolve to different hosts here; the guard must check the push
    host (github.sec.samsung.net), so dropping `--push` (reverting to the fetch URL,
    github.com) would fail this test."""
    create_pr = _load_create_pr()
    seen = {"hostname": None}

    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "remote", "get-url"]:
            if "--push" in argv:
                return SimpleNamespace(returncode=0, stdout="https://github.sec.samsung.net/bdp/x.git", stderr="")
            return SimpleNamespace(returncode=0, stdout="https://github.com/o/r.git", stderr="")
        if argv[:2] == ["gh", "auth"]:
            seen["hostname"] = argv[argv.index("--hostname") + 1]
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # unauth → error path
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(create_pr.subprocess, "run", fake_run)
    monkeypatch.setattr(create_pr, "compute_repo_diff", lambda cwd: "")
    monkeypatch.setattr(create_pr, "repo_root", lambda: Path("/repo"))
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert seen["hostname"] == "github.sec.samsung.net"  # push host, NOT the fetch host github.com
    assert "github.sec.samsung.net" in result["feedback"]
    assert calls["invoked"] is False


def test_gh_auth_timeout_fails_closed_without_invoking_worker(monkeypatch, tmp_path):
    """IR-001: the preflight must not become the new hang. A `gh auth status` that
    hangs (TimeoutExpired) fails closed fast with guidance — never reaching the
    worker, never out-waiting the 900s worker timeout it pre-empts."""
    create_pr = _load_create_pr()
    _patch_git_remote(create_pr, monkeypatch, remote_url="https://github.sec.samsung.net/bdp/x.git", gh_timeout=True)
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert result["status"] == "error"
    assert "timed out" in result["feedback"] and "github.sec.samsung.net" in result["feedback"]
    assert calls["invoked"] is False


def test_authenticated_host_proceeds_to_worker(monkeypatch, tmp_path):
    """A reachable, authenticated host passes the preflight and the worker IS
    invoked (it then fails on missing pr_url, but the preflight did not short it)."""
    create_pr = _load_create_pr()
    _patch_git_remote(create_pr, monkeypatch, remote_url="https://github.com/o/r.git", gh_rc=0)
    monkeypatch.setattr(
        create_pr,
        "load_config",
        lambda rr: SimpleNamespace(
            project=SimpleNamespace(
                branch_prefix="redteam", base_branch="main", test_dir="tests/", source_dirs=["src/"]
            )
        ),
    )
    monkeypatch.setattr(create_pr, "read_text_if_exists", lambda p: None)
    calls, get = _worker_spy()
    monkeypatch.setattr(create_pr, "get_worker_adapter", get)

    result = create_pr.run(tmp_path / "task-001", {})

    assert calls["invoked"] is True  # preflight passed → agent ran
    assert result["status"] == "error"  # no pr_url produced in the stub, but that's the post-agent path
