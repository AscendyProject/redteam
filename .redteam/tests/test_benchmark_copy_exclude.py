"""Tests for #185 — a benchmark set can opt into copying its virtualenv.

The reported failure: `run_one` hardcoded `venv`/`.venv` into its copytree
exclusions, so a repo whose verify_command activates a project-local virtualenv
found no tools on PATH in the tempcopy. Every verification exec exited 127, the
task churned through its retries to `deferred`, and the run recorded metrics that
described a broken environment rather than a model combination — at full cost.

The fix splits the exclusion list in two: a mandatory half a set cannot drop
(`.git`, `batches`, `results`, `results.jsonl` — dropping any of them hands the
tempcopy real history or real batch state) and a default half a set may replace
via `copy_exclude` in benchmark.toml.

All tests are hermetic: the "repo" is a small tmp_path tree, subprocess is stubbed,
and copytree is spied on so the assertions are about what actually landed in the
snapshot rather than about the opaque ignore callable.
"""

from __future__ import annotations

import shutil
import subprocess as real_subprocess
import sys
import types
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import pytest  # noqa: E402

import benchmark as bm  # noqa: E402
from benchmark import DEFAULT_COPY_EXCLUDE, load_benchmark_set  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_set(root: Path, *, copy_exclude_line: str = "") -> None:
    """A minimal one-config, one-task benchmark set."""
    root.mkdir(parents=True, exist_ok=True)
    body = '[configs.default]\nplanner = "model-x"\n'
    (root / "benchmark.toml").write_text(copy_exclude_line + body, encoding="utf-8")
    task_dir = root / "tasks" / "task-001"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.md").write_text("some input", encoding="utf-8")


def _fake_repo(root: Path) -> Path:
    """A tiny stand-in for the operator's repo, carrying one file per exclusion class.

    The real .redteam/config.toml is reused verbatim so the merge step under test
    operates on a realistic document instead of a guessed one.
    """
    (root / ".redteam").mkdir(parents=True)
    shutil.copy(str(bm._repo_root() / ".redteam" / "config.toml"), str(root / ".redteam" / "config.toml"))

    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "ruff").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker").write_text("x", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "stale.pyc").write_text("x", encoding="utf-8")

    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "batches" / "real-batch").mkdir(parents=True)
    (root / "batches" / "real-batch" / "state.json").write_text("{}", encoding="utf-8")
    (root / "results").mkdir()
    (root / "results" / "old.txt").write_text("x", encoding="utf-8")
    (root / "results.jsonl").write_text("{}\n", encoding="utf-8")

    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return root


def _snapshot_via_run_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_root: Path) -> set[str]:
    """Run run_one against a fake repo; return the repo-relative paths that were copied.

    The tempcopy is deleted when run_one returns, so the spy records the snapshot's
    contents while it still exists.
    """
    repo = _fake_repo(tmp_path / "repo")
    monkeypatch.setattr(bm, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        bm,
        "subprocess",
        types.SimpleNamespace(
            run=lambda args, **kw: real_subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        ),
    )

    copied: set[str] = set()

    def spy(src, dst, **kwargs):
        result = shutil.copytree(src, dst, **kwargs)
        root = Path(dst)
        copied.update(str(p.relative_to(root)) for p in root.rglob("*"))
        return result

    # Swap the module reference benchmark.py holds, not shutil.copytree itself:
    # copytree recurses through its own module global, so patching it in place
    # would re-enter the spy for every subdirectory.
    monkeypatch.setattr(
        bm,
        "shutil",
        types.SimpleNamespace(copytree=spy, ignore_patterns=shutil.ignore_patterns, copy2=shutil.copy2),
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bm.run_one(
        set_root=set_root,
        config_name="default",
        task_id="task-001",
        repetition=1,
        config_overrides={"planner": "model-x"},
        workspace=workspace,
    )
    return copied


# ---------------------------------------------------------------------------
# run_one — what actually lands in the snapshot
# ---------------------------------------------------------------------------


def test_set_opts_into_its_virtualenv_but_cannot_drop_the_mandatory_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#185: `copy_exclude` restores the venv, and the mandatory half survives it.

    The venv half fails against pre-change code, where `venv`/`.venv` were
    hardcoded into the ignore patterns and no set could get them copied.

    The mandatory half is asserted in the same test on purpose: the set below
    deliberately omits `.git`, `batches`, `results` and `results.jsonl` from its
    `copy_exclude`, so if the new key simply replaced the whole list, real git
    history and real batch state would land in the tempcopy — a trust root and a
    resumable batch the run must never see. Alone that assertion could not
    discriminate (pre-change they were excluded too); paired with the venv half it
    pins that the override widened exactly as far as intended and no further.
    """
    set_root = tmp_path / "bset"
    _write_set(set_root, copy_exclude_line='copy_exclude = ["__pycache__"]\n')

    copied = _snapshot_via_run_one(tmp_path, monkeypatch, set_root)

    # Opted in: the virtualenv is present, so a venv-based verify_command works.
    assert "venv/bin/ruff" in copied
    assert ".venv/marker" in copied
    # Still honoured from the set's own list.
    assert not any(p.startswith("__pycache__") for p in copied)
    # Mandatory, despite being absent from copy_exclude.
    assert not any(p == ".git" or p.startswith(".git/") for p in copied)
    assert not any(p.startswith("batches") for p in copied)
    assert not any(p.startswith("results") for p in copied)
    assert "results.jsonl" not in copied
    # Ordinary sources are unaffected.
    assert "src/app.py" in copied


def test_default_set_still_excludes_the_virtualenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `copy_exclude` keeps the pre-#185 snapshot exactly.

    The fix is opt-in: a set that says nothing must not silently start copying a
    51M virtualenv into every run.
    """
    set_root = tmp_path / "bset"
    _write_set(set_root)

    copied = _snapshot_via_run_one(tmp_path, monkeypatch, set_root)

    assert not any(p.startswith("venv") for p in copied)
    assert not any(p.startswith(".venv") for p in copied)
    assert not any(p.startswith("__pycache__") for p in copied)
    assert "src/app.py" in copied


# ---------------------------------------------------------------------------
# load_benchmark_set — parsing and validation
# ---------------------------------------------------------------------------


def test_copy_exclude_parsing_default_override_and_empty(tmp_path: Path) -> None:
    """Absent → default; present → replaces it; explicit [] is honoured, not treated
    as "unset" (falsiness would silently re-apply the default and defeat the opt-in)."""
    absent = tmp_path / "absent"
    _write_set(absent)
    assert load_benchmark_set(absent).copy_exclude == DEFAULT_COPY_EXCLUDE

    override = tmp_path / "override"
    _write_set(override, copy_exclude_line='copy_exclude = ["node_modules", "*.egg-info"]\n')
    assert load_benchmark_set(override).copy_exclude == ("node_modules", "*.egg-info")

    empty = tmp_path / "empty"
    _write_set(empty, copy_exclude_line="copy_exclude = []\n")
    assert load_benchmark_set(empty).copy_exclude == ()


@pytest.mark.parametrize(
    "line",
    [
        'copy_exclude = "venv"\n',  # a bare string is not a list
        "copy_exclude = 3\n",
        "copy_exclude = [true]\n",  # bool is not a name
        "copy_exclude = [1]\n",
        'copy_exclude = [""]\n',  # empty pattern would match nothing meaningful
        'copy_exclude = ["   "]\n',
    ],
)
def test_copy_exclude_rejects_malformed_values(tmp_path: Path, line: str) -> None:
    """Bad shapes fail loudly at load time, before any money is spent — a silently
    ignored typo here would produce a snapshot nobody intended."""
    set_root = tmp_path / "bset"
    _write_set(set_root, copy_exclude_line=line)

    with pytest.raises(ValueError, match="copy_exclude"):
        load_benchmark_set(set_root)
