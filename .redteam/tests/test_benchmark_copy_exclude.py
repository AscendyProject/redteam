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

    # A venv shaped like a real one: activate exports an absolute VIRTUAL_ENV and
    # console scripts carry an absolute shebang. Both are what make a copied venv
    # non-relocatable, so they are modelled rather than stubbed.
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "ruff").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "venv" / "bin" / "activate").write_text(
        f'export VIRTUAL_ENV={root / "venv"}\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n', encoding="utf-8"
    )
    (root / "venv" / "bin" / "pytest").write_text(f"#!{root / 'venv' / 'bin' / 'python'}\n", encoding="utf-8")
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


def _snapshot_via_run_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_root: Path, name: str = "repo"
) -> types.SimpleNamespace:
    """Run run_one against a fake repo; report what landed in the snapshot.

    Returns .paths (repo-relative names copied), .texts (contents of the venv entry
    points, plus "__root__" = the snapshot dir) and .origin (the fake repo). The
    tempcopy is deleted when run_one returns, so the spy records while it still
    exists. `name` keeps repeated calls in one test isolated.
    """
    repo = _fake_repo(tmp_path / name)
    monkeypatch.setattr(bm, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        bm,
        "subprocess",
        types.SimpleNamespace(
            run=lambda args, **kw: real_subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        ),
    )

    copied: set[str] = set()
    texts: dict[str, str] = {}

    def spy(src, dst, **kwargs):
        result = shutil.copytree(src, dst, **kwargs)
        root = Path(dst)
        copied.update(str(p.relative_to(root)) for p in root.rglob("*"))
        # Read while the snapshot still exists — run_one deletes it on return.
        for rel in ("venv/bin/activate", "venv/bin/pytest"):
            if (root / rel).is_file():
                texts[rel] = (root / rel).read_text(encoding="utf-8")
        texts["__root__"] = str(root)
        return result

    # Swap the module reference benchmark.py holds, not shutil.copytree itself:
    # copytree recurses through its own module global, so patching it in place
    # would re-enter the spy for every subdirectory.
    monkeypatch.setattr(
        bm,
        "shutil",
        types.SimpleNamespace(copytree=spy, ignore_patterns=shutil.ignore_patterns, copy2=shutil.copy2),
    )

    workspace = tmp_path / f"workspace-{name}"
    workspace.mkdir()
    bm.run_one(
        set_root=set_root,
        config_name="default",
        task_id="task-001",
        repetition=1,
        config_overrides={"planner": "model-x"},
        workspace=workspace,
    )
    return types.SimpleNamespace(paths=copied, texts=texts, origin=repo)


# ---------------------------------------------------------------------------
# run_one — what actually lands in the snapshot
# ---------------------------------------------------------------------------


def test_copy_exclude_is_what_flips_the_virtualenv_into_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#185: the same repo, snapshotted twice, differs only by the set's `copy_exclude`.

    Both halves are asserted here rather than in two tests because the default half
    on its own merely characterizes pre-change behaviour (#185 review IR-001) — it
    passes against the old hardcoded list. Run as a contrast against an identical
    fake repo, it becomes the control that proves the new key is the cause: nothing
    about the repo changed between the two snapshots, only the toml.

    The opt-in half fails against pre-change code, where `venv`/`.venv` were
    hardcoded into the ignore patterns and no set could get them copied.
    """
    default_set = tmp_path / "bset-default"
    _write_set(default_set)
    opt_in_set = tmp_path / "bset-optin"
    _write_set(opt_in_set, copy_exclude_line='copy_exclude = ["__pycache__"]\n')

    default_copy = _snapshot_via_run_one(tmp_path, monkeypatch, default_set, name="repo-default").paths
    opt_in_copy = _snapshot_via_run_one(tmp_path, monkeypatch, opt_in_set, name="repo-optin").paths

    # Control: saying nothing keeps the pre-#185 snapshot, so a set that has not
    # opted in does not silently start copying a 51M virtualenv into every run.
    assert not any(p.startswith("venv") or p.startswith(".venv") for p in default_copy)
    # Treatment: the only difference is the toml key.
    assert "venv/bin/ruff" in opt_in_copy
    assert ".venv/marker" in opt_in_copy

    # Both snapshots are otherwise the same, and both still honour __pycache__ —
    # the opt-in set kept it, so the override is a replacement, not a blanket off-switch.
    for copied in (default_copy, opt_in_copy):
        assert "src/app.py" in copied
        assert not any(p.startswith("__pycache__") for p in copied)


def test_mandatory_exclusions_survive_a_copy_exclude_that_omits_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set cannot drop `.git` / `batches` / `results` by leaving them out.

    The set below lists only `__pycache__`, so if the new key simply replaced the
    whole list, real git history and real batch state would land in the tempcopy —
    a trust root for the pre-implement floors and a resumable batch the run must
    never see. The fake repo carries a real-looking file in each of those paths, so
    this fails against a replacement-semantics implementation.
    """
    set_root = tmp_path / "bset"
    _write_set(set_root, copy_exclude_line='copy_exclude = ["__pycache__"]\n')

    copied = _snapshot_via_run_one(tmp_path, monkeypatch, set_root).paths

    assert not any(p == ".git" or p.startswith(".git/") for p in copied)
    assert not any(p.startswith("batches") for p in copied)
    assert not any(p.startswith("results") for p in copied)
    assert "results.jsonl" not in copied


def test_copied_virtualenv_still_resolves_to_the_original_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#185 review IR-001: pin how far the snapshot's isolation actually reaches.

    Copying a venv does NOT make it relocatable — bin/activate exports an absolute
    VIRTUAL_ENV and console scripts carry an absolute shebang, both baked at
    creation time. So verification inside the snapshot executes the HOST's
    interpreter and tools. Measured against this repo's real venv:

        VIRTUAL_ENV=/Users/kh/Documents/redteam/venv
        which pytest=/Users/kh/Documents/redteam/venv/bin/pytest
        sys.executable=/Users/kh/Documents/redteam/venv/bin/python

    Asserting that here rather than only in a docstring makes the boundary
    executable: whoever later makes the copy relocatable — or switches to
    provisioning an environment inside the snapshot — trips this test and is forced
    to update the isolation claim in run_one's docstring along with it.

    It is deliberately NOT a test that the tools resolve *inside* the snapshot;
    that would assert something false. See the PR discussion for why the two
    achievable-looking remedies are not: rewriting the absolute paths is the
    unreliable trick upstream removed with `virtualenv --relocatable`, and
    provisioning a fresh environment needs a stack-specific install command the
    engine must not encode.
    """
    set_root = tmp_path / "bset"
    _write_set(set_root, copy_exclude_line='copy_exclude = ["__pycache__"]\n')

    snap = _snapshot_via_run_one(tmp_path, monkeypatch, set_root)

    origin_venv = str(snap.origin / "venv")
    snapshot_venv = snap.texts["__root__"] + "/venv"
    assert origin_venv != snapshot_venv, "fixture bug: the snapshot must be a different directory"

    # The copy is present (so verify.sh takes its activate branch at all) ...
    assert "venv/bin/activate" in snap.texts
    # ... but every entry point still points at the environment it was created in.
    assert f"VIRTUAL_ENV={origin_venv}" in snap.texts["venv/bin/activate"]
    assert snapshot_venv not in snap.texts["venv/bin/activate"]
    assert snap.texts["venv/bin/pytest"].startswith(f"#!{origin_venv}/bin/python")


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
    ignored typo here would produce a snapshot nobody intended.

    #185 review IR-001: the match is pinned to the *type-validation* messages, not
    the substring "copy_exclude". Pre-change the key was unknown at top level, so a
    loose match was satisfied by "Unknown top-level key(s) ... ['copy_exclude']" —
    the test passed for the wrong reason and proved nothing about the new
    validation running. That the key is otherwise accepted is established
    independently by test_copy_exclude_parsing_default_override_and_empty, which
    parses a valid list and itself fails pre-change on the unknown-key rejection.
    """
    set_root = tmp_path / "bset"
    _write_set(set_root, copy_exclude_line=line)

    with pytest.raises(ValueError, match=r"copy_exclude (must be a list of strings|entries must be non-empty strings)"):
        load_benchmark_set(set_root)
