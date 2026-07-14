"""Tests for plan-declared Affected files exemption in _floor_outside_scope (#137).

Covers _plan_affected_files, _get_or_set_plan_affected_files_baseline, and
_floor_outside_scope's plan_affected keyword-only parameter:
- Exemption on: plan_affected path skips the floor
- Exemption off: default empty frozenset → same path trips the floor
- Parser: (new) prefix stripped case-insensitively and positionally
- Parser: absent / no-heading → empty frozenset (fail-closed)
- Parser: malformed entries skipped; well-formed honored in the same list
- Parser: heading boundary — same-or-higher-level heading stops the section
- Parser: exact equality — trailing slash is NOT a directory prefix
- Set-once: same-process second round ignores widened outcome.md
- Set-once: fresh-process re-entry (state.json round-trip) ignores widened outcome.md
- Cross-run trust-root floor NOT exempted by plan_affected (security boundary)
- Default byte-identical for in-scope-only tasks
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _impl():
    import _engine

    return _engine.implement()


_PROJ = SimpleNamespace(
    source_dirs=["app/"],
    test_dir="tests/",
    context_file="docs/ctx.md",
    base_branch="main",
)


def _make_task_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (cwd, task_dir) with a basic repo layout."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "app").mkdir()
    (cwd / "tests").mkdir()
    task_dir = cwd / ".redteam" / "batches" / "b" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    return cwd, task_dir


# =============================================================================
# 1. Exemption ON — plan_affected contains the path
# =============================================================================


def test_plan_affected_exemption_on(tmp_path):
    """Tracked path outside scope is NOT in the offending set when plan_affected contains it."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    doc_path = "docs/reviewer/round-staging.md"
    plan_affected = frozenset({doc_path})

    result = impl._floor_outside_scope({doc_path}, _PROJ, task_dir, cwd, plan_affected=plan_affected)

    assert result == set(), f"plan_affected path should be exempt; got {result}"


# =============================================================================
# 2. Exemption OFF — default empty frozenset (fail-closed)
# =============================================================================


def test_plan_affected_exemption_off_default(tmp_path):
    """Same path IS in the offending set when plan_affected is empty (default)."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    doc_path = "docs/reviewer/round-staging.md"

    result = impl._floor_outside_scope({doc_path}, _PROJ, task_dir, cwd)

    assert doc_path in result, "without plan_affected, outside-scope path must trip the floor"


# =============================================================================
# 3. Parser — (new) prefix stripping (case-insensitive, positional)
# =============================================================================


def test_parser_new_prefix_lowercase_stripped(tmp_path):
    """'(new) ' prefix (lowercase) is stripped to give the bare path."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- (new) docs/x.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/x.md" in result, "(new) prefix must be stripped"
    assert not any(p.startswith("(new)") for p in result), "no entry should start with '(new)'"


def test_parser_new_prefix_mixed_case_stripped(tmp_path):
    """'(New) ' prefix (mixed case) is stripped case-insensitively."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- (New) docs/y.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/y.md" in result, "(New) prefix must be stripped case-insensitively"


def test_parser_new_suffix_not_stripped(tmp_path):
    """'(new)' NOT at the start of the item stays as a literal part of the path."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- foo (new).md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "foo (new).md" in result, "non-leading (new) must not be stripped"


def test_parser_new_prefix_inside_backticks(tmp_path):
    """'`(new) docs/x.md`' — backtick-wrapped entry with prefix — yields bare path."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- `(new) docs/x.md`\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/x.md" in result, "backtick-wrapped (new) entry must yield bare path"


# =============================================================================
# 4. Parser — absent / empty outcome.md / no heading (fail-closed)
# =============================================================================


def test_parser_absent_outcome_md(tmp_path):
    """Absent outcome.md → empty frozenset."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    result = impl._plan_affected_files(task_dir)

    assert result == frozenset()


def test_parser_no_affected_files_heading(tmp_path):
    """outcome.md with no Affected files heading → empty frozenset."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "# Task\n\n## Goals\n- foo\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert result == frozenset()


def test_parser_empty_affected_files_section(tmp_path):
    """outcome.md with an empty Affected files section → empty frozenset."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "# Task\n\n## Affected files\n\n## Next\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert result == frozenset()


def test_parser_unreadable_outcome_md(tmp_path):
    """Non-UTF-8 (undecodable) outcome.md → empty frozenset (fail-closed)."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    # Write raw bytes that are not valid UTF-8
    (task_dir / "outcome.md").write_bytes(b"\xff\xfe## Affected files\r\n- \xff\xfe\r\n")

    result = impl._plan_affected_files(task_dir)

    assert result == frozenset(), "undecodable outcome.md must yield empty frozenset (fail-closed)"


def test_parser_absent_outcome_md_floor_unchanged(tmp_path):
    """Absent outcome.md → offending set byte-identical to baseline (no plan_affected)."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    doc_path = "docs/reviewer/round-staging.md"

    # Baseline (no outcome.md, no plan_affected passed)
    baseline_result = impl._floor_outside_scope({doc_path}, _PROJ, task_dir, cwd)

    # With empty plan_affected explicitly
    with_empty = impl._floor_outside_scope({doc_path}, _PROJ, task_dir, cwd, plan_affected=frozenset())

    assert baseline_result == with_empty == {doc_path}


# =============================================================================
# 5. Parser — malformed entries skipped; well-formed honored
# =============================================================================


def test_parser_absolute_path_skipped_well_formed_honored(tmp_path):
    """Absolute path entry is skipped; well-formed entry in the same list is kept."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- /etc/passwd\n- docs/good.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "/etc/passwd" not in result, "absolute path must be skipped"
    assert "docs/good.md" in result, "well-formed path must be honored"


def test_parser_dotdot_segment_skipped_well_formed_honored(tmp_path):
    """Entry with '..' segment is skipped; well-formed entry in the same list is kept."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- ../evil\n- docs/good.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "../evil" not in result, "'..' segment must be skipped"
    assert "docs/good.md" in result, "well-formed path must be honored"


# =============================================================================
# 6. Parser — heading boundary (same-or-higher-level heading stops the section)
# =============================================================================


def test_parser_same_level_heading_stops_section(tmp_path):
    """Bullets after a same-level heading do not bleed into the exemption set."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- docs/in-scope.md\n\n## Out of scope\n- docs/out-of-scope.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/in-scope.md" in result
    assert "docs/out-of-scope.md" not in result, "bullets after same-level heading must not bleed"


def test_parser_higher_level_heading_stops_section(tmp_path):
    """A higher-level heading (fewer #) stops the section parse."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "### Affected files\n- docs/in-scope.md\n\n## Higher level\n- docs/not-included.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/in-scope.md" in result
    assert "docs/not-included.md" not in result, "bullets after higher-level heading must not bleed"


def test_parser_subheading_does_not_stop_section(tmp_path):
    """A sub-heading (more #) does NOT stop the section; bullets still collected."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- docs/a.md\n\n#### Sub-note\n- docs/b.md\n\n## Next\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    # Both bullets are within the "Affected files" section (#### < ## stops nothing)
    assert "docs/a.md" in result


# =============================================================================
# 7. Parser — exact equality, no prefix expansion
# =============================================================================


def test_parser_trailing_slash_not_directory_prefix(tmp_path):
    """'docs/reviewer/' (trailing slash) in Affected files does NOT exempt children."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- docs/reviewer/\n",
        encoding="utf-8",
    )

    plan_affected = impl._plan_affected_files(task_dir)
    child_path = "docs/reviewer/round-staging.md"

    result = impl._floor_outside_scope({child_path}, _PROJ, task_dir, cwd, plan_affected=plan_affected)

    assert child_path in result, "trailing-slash entry must not exempt paths under that directory"


# =============================================================================
# 8. Set-once — same-process second round
# =============================================================================


def test_set_once_same_process_widened_outcome_ignored(tmp_path):
    """Second call in-process with key present ignores a widened outcome.md."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    path_a = "docs/reviewer/round-staging.md"
    path_b = "docs/reviewer/other.md"

    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {path_a}\n",
        encoding="utf-8",
    )

    state: dict[str, Any] = {}
    first = impl._get_or_set_plan_affected_files_baseline(state, task_dir)

    assert path_a in first
    assert path_b not in first
    assert isinstance(state.get("implement_plan_affected_files"), list)

    # Widen the live outcome.md to add path_b
    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {path_a}\n- {path_b}\n",
        encoding="utf-8",
    )

    second = impl._get_or_set_plan_affected_files_baseline(state, task_dir)

    assert path_a in second
    assert path_b not in second, "widened outcome.md must not affect the stored snapshot"
    assert state["implement_plan_affected_files"] == sorted({path_a})


# =============================================================================
# 9. Set-once — fresh-process re-entry (state.json round-trip)
# =============================================================================


def test_set_once_fresh_process_reentry(tmp_path):
    """Reloaded state.json has key present → parser not re-called; widened outcome.md ignored."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    path_a = "docs/reviewer/round-staging.md"
    path_b = "docs/reviewer/other.md"

    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {path_a}\n",
        encoding="utf-8",
    )

    state: dict[str, Any] = {}
    impl._get_or_set_plan_affected_files_baseline(state, task_dir)

    # Simulate persist_state + fresh process load
    state_file = task_dir / "state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    reloaded = json.loads(state_file.read_text(encoding="utf-8"))

    # Widen outcome.md after persisting
    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {path_a}\n- {path_b}\n",
        encoding="utf-8",
    )

    result = impl._get_or_set_plan_affected_files_baseline(reloaded, task_dir)

    assert path_a in result
    assert path_b not in result, "stored key must prevent re-parsing after fresh-process reload"


def test_set_once_empty_parse_stored_and_not_reparsed(tmp_path):
    """Fail-closed empty parse (no heading) is stored as [] and never re-parsed."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    # outcome.md with no Affected files heading → empty parse
    (task_dir / "outcome.md").write_text("# Goals\n- foo\n", encoding="utf-8")

    state: dict[str, Any] = {}
    first = impl._get_or_set_plan_affected_files_baseline(state, task_dir)

    assert first == frozenset()
    assert state["implement_plan_affected_files"] == []

    # Now add an Affected files section to the live file
    (task_dir / "outcome.md").write_text(
        "## Affected files\n- docs/sneaky.md\n",
        encoding="utf-8",
    )

    # Second call: key present → empty list → return empty frozenset (not re-parsed)
    second = impl._get_or_set_plan_affected_files_baseline(state, task_dir)

    assert second == frozenset(), "empty-parse stored snapshot must not be re-parsed"
    assert "docs/sneaky.md" not in second


# =============================================================================
# 10. Cross-run trust-root floor NOT exempted by plan_affected (security boundary)
# =============================================================================


def test_cross_run_floor_not_exempted_by_plan_affected(tmp_path):
    """_cross_run_trust_root_floor Check-2 catches outside-scope stored-baseline entries
    even when outcome.md lists the path under Affected files."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    outside_path = "docs/reviewer/round-staging.md"

    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {outside_path}\n",
        encoding="utf-8",
    )

    # Adversarially-injected stored baseline entry
    state = {"implement_tracked_baseline": [outside_path]}

    result = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, set())

    assert outside_path in result, (
        "_cross_run_trust_root_floor Check-2 must still catch outside-scope baseline entries "
        "even when outcome.md names them (plan_affected must not weaken this floor)"
    )


def test_cross_run_floor_check1_not_exempted_by_plan_affected(tmp_path):
    """_cross_run_trust_root_floor Check-1 catches outside-scope untracked paths
    even when outcome.md lists the path."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    outside_path = "docs/reviewer/round-staging.md"

    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {outside_path}\n",
        encoding="utf-8",
    )

    state: dict[str, Any] = {}

    result = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, {outside_path})

    assert outside_path in result, (
        "_cross_run_trust_root_floor Check-1 must catch outside-scope untracked paths even when outcome.md names them"
    )


# =============================================================================
# 11. Default byte-identical — in-scope-only tasks unaffected
# =============================================================================


def test_default_in_scope_path_always_exempt(tmp_path):
    """In-scope paths under source_dirs never appear in the offending set regardless of outcome.md."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    in_scope_path = "app/module.py"

    # Without outcome.md
    result_absent = impl._floor_outside_scope({in_scope_path}, _PROJ, task_dir, cwd)
    assert result_absent == set(), "in-scope path must never be offending (no outcome.md)"

    # With outcome.md present and listing a different path
    (task_dir / "outcome.md").write_text(
        "## Affected files\n- docs/other.md\n",
        encoding="utf-8",
    )
    plan_affected = impl._plan_affected_files(task_dir)
    result_with_outcome = impl._floor_outside_scope({in_scope_path}, _PROJ, task_dir, cwd, plan_affected=plan_affected)
    assert result_with_outcome == set(), "in-scope path must never be offending (outcome.md present)"


def test_default_path_empty_current_tracked(tmp_path):
    """Empty current_tracked → empty offending set regardless of plan_affected."""
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)

    plan_affected = frozenset({"docs/reviewer/round-staging.md"})

    result = impl._floor_outside_scope(set(), _PROJ, task_dir, cwd, plan_affected=plan_affected)

    assert result == set()


# =============================================================================
# 12. _uncommitted_plan_affected_paths — unit tests (IR-001)
# =============================================================================


def test_uncommitted_plan_affected_empty_fast_path(tmp_path):
    """Empty plan_affected fast-path returns empty set without any git call."""
    impl = _impl()
    cwd = tmp_path

    result = impl._uncommitted_plan_affected_paths(cwd, frozenset())

    assert result == set()


def test_uncommitted_plan_affected_detects_unstaged_path(monkeypatch, tmp_path):
    """A plan-affected path that appears in `git diff` (unstaged) is returned."""
    impl = _impl()
    cwd = tmp_path
    doc_path = "docs/reviewer/round-staging.md"
    plan_affected = frozenset({doc_path})

    def fake_git(args, cwd_arg):
        # unstaged probe
        if "--cached" not in args:
            return SimpleNamespace(returncode=0, stdout=doc_path + "\0", stderr="")
        # staged probe
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(impl, "run_git_checked", fake_git)
    result = impl._uncommitted_plan_affected_paths(cwd, plan_affected)

    assert result == {doc_path}


def test_uncommitted_plan_affected_detects_staged_path(monkeypatch, tmp_path):
    """A plan-affected path that appears in `git diff --cached` (staged) is returned."""
    impl = _impl()
    cwd = tmp_path
    doc_path = "docs/reviewer/round-staging.md"
    plan_affected = frozenset({doc_path})

    def fake_git(args, cwd_arg):
        # staged probe
        if "--cached" in args:
            return SimpleNamespace(returncode=0, stdout=doc_path + "\0", stderr="")
        # unstaged probe
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(impl, "run_git_checked", fake_git)
    result = impl._uncommitted_plan_affected_paths(cwd, plan_affected)

    assert result == {doc_path}


def test_uncommitted_plan_affected_clean_path_not_returned(monkeypatch, tmp_path):
    """A plan-affected path NOT in git diff is NOT returned (clean path)."""
    impl = _impl()
    cwd = tmp_path
    doc_path = "docs/reviewer/round-staging.md"
    plan_affected = frozenset({doc_path})

    def fake_git(args, cwd_arg):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(impl, "run_git_checked", fake_git)
    result = impl._uncommitted_plan_affected_paths(cwd, plan_affected)

    assert result == set()


def test_uncommitted_plan_affected_other_dirty_path_not_returned(monkeypatch, tmp_path):
    """A dirty path NOT in plan_affected is NOT returned (only plan_affected paths matter)."""
    impl = _impl()
    cwd = tmp_path
    doc_path = "docs/reviewer/round-staging.md"
    other_path = "docs/some-other.md"
    plan_affected = frozenset({doc_path})

    def fake_git(args, cwd_arg):
        # other_path is dirty, doc_path is clean
        return SimpleNamespace(returncode=0, stdout=other_path + "\0", stderr="")

    monkeypatch.setattr(impl, "run_git_checked", fake_git)
    result = impl._uncommitted_plan_affected_paths(cwd, plan_affected)

    assert result == set(), "only plan_affected paths should be checked"


# =============================================================================
# 13. IR-001 regression — integrity gate refuses uncommitted plan-affected path
# =============================================================================


def _wire_agent_pair(impl, monkeypatch, cwd):
    """Minimal stubs for _run_agent_pair: repo_root, project_config, worker adapter,
    verification commands."""
    monkeypatch.setattr(impl, "repo_root", lambda: cwd)
    monkeypatch.setattr(impl, "project_config", lambda: _PROJ)
    monkeypatch.setattr(
        impl,
        "get_worker_adapter",
        lambda state: SimpleNamespace(invoke=lambda **kw: {"returncode": 0, "stdout": "done", "stderr": ""}),
    )
    monkeypatch.setattr(
        impl,
        "_run_verification_commands",
        lambda cwd, commands, project_verify_command=None, allowlist=None: (0, "ok\n"),
    )


def _ok_proc(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_ir001_integrity_gate_refuses_uncommitted_plan_affected_path(monkeypatch, tmp_path):
    """IR-001 regression: when a plan-affected outside-scope path was dirty-vs-base
    BEFORE the first implement round, _commit_worker_diff excludes it (it is in
    before_tracked), and the integrity gate (layer 3) must refuse — verify must
    never be green while the committed range omits the worker's change.

    Simulation: doc_path is in plan_affected (floor exempt) AND in before_tracked
    (dirty before worker). _commit_worker_diff stages tracked_delta - before_tracked
    = {} → nothing committed → doc_path still shows in git diff → layer 3 fires.
    """
    impl = _impl()
    cwd, task_dir = _make_task_layout(tmp_path)
    doc_path = "docs/reviewer/round-staging.md"

    (task_dir / "outcome.md").write_text(
        f"## Affected files\n- {doc_path}\n",
        encoding="utf-8",
    )

    _wire_agent_pair(impl, monkeypatch, cwd)

    def fake_run(argv, **kwargs):
        has_c = "-c" in argv

        # Untracked probe (ls-files --others): always empty
        if "ls-files" in argv and "--others" in argv:
            return _ok_proc("")

        # Tracked probe via run_git_checked (-c present): doc_path dirty (before AND after worker)
        if has_c and "diff" in argv and "-z" in argv and "--name-only" in argv:
            return _ok_proc(doc_path + "\0")

        # _uncommitted_scope_files probes (no -c): diff --name-only -z
        # doc_path not under app/ or tests/ → filtered out by _uncommitted_scope_files → safe to return it
        if not has_c and "diff" in argv and "--name-only" in argv and "-z" in argv:
            return _ok_proc(doc_path + "\0")

        # Branch diff probes (_branch_diff_checked, no -z, no --name-only, no --quiet)
        if "diff" in argv and "-z" not in argv and "--name-only" not in argv and "--quiet" not in argv:
            return _ok_proc("")

        # commit_paths: --literal-pathspecs diff --cached --quiet (but to_stage=[] → never called)
        # git add / git commit (also not called with empty to_stage)
        return _ok_proc("")

    monkeypatch.setattr(impl.subprocess, "run", fake_run)

    state = {
        "task_id": "task-002",
        "mode": "agent-pair",
        "base_branch": "main",
        "verification": {"commands": []},
    }
    result = impl._run_agent_pair(task_dir, state)

    assert result["status"] == "error", (
        f"integrity gate must refuse when a plan-affected path is dirty after commit; got {result['status']!r}"
    )
    assert doc_path in result["feedback"], "feedback must name the uncommitted plan-affected path"


# =============================================================================
# 14. Parser — backtick form with description separator regression (#149)
# =============================================================================


def test_parser_backtick_form_em_dash_separator(tmp_path):
    """Standard backtick form '- `path` — reason' (em-dash separator) extracts the exact path (#149)."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- `.redteam/templates/x.json` — one-line reason\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert ".redteam/templates/x.json" in result, "backtick form with em-dash separator must parse to the exact path"
    assert not any("reason" in p for p in result), "description residue must not appear in extracted path"
    assert not any(p.endswith("`") for p in result), "trailing backtick must not appear in extracted path"


def test_parser_backtick_new_inside_with_em_dash_separator(tmp_path):
    """Standard `- `(new) path` — reason` form: (new) inside backtick + em-dash separator."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- `(new) .redteam/templates/x.json` — new file added\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert ".redteam/templates/x.json" in result
    assert not any("(new)" in p for p in result), "(new) prefix must not appear in extracted path"
    assert not any("reason" in p for p in result), "description residue must not appear in extracted path"


def test_parser_bare_path_no_separator(tmp_path):
    """Bare `- path/to/file` (no description, no backticks) parses to that exact path."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- .redteam/workflows/impl.py\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert ".redteam/workflows/impl.py" in result


def test_parser_bare_path_hyphen_separator(tmp_path):
    """Bare `- path/to/file - reason` (hyphen separator) parses to the path only."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- .redteam/workflows/impl.py - modify the parser\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert ".redteam/workflows/impl.py" in result
    assert not any("modify" in p for p in result), "description residue must not appear in extracted path"


def test_parser_adversarial_multiple_backtick_spans_yields_only_first(tmp_path):
    """Adversarial: bullet with two backtick spans yields only the first — never both (#149)."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- `docs/a.md` and `docs/b.md` — dual path attempt\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    assert "docs/a.md" in result, "first backtick span must be extracted"
    assert "docs/b.md" not in result, "second backtick span must not produce a second exemption"


def test_parser_adversarial_empty_backtick_span_skipped(tmp_path):
    """Adversarial: empty backtick span is skipped (fail-closed) — no over-exemption (#149)."""
    impl = _impl()
    _, task_dir = _make_task_layout(tmp_path)

    (task_dir / "outcome.md").write_text(
        "## Affected files\n- `` — reason after empty span\n- docs/good.md\n",
        encoding="utf-8",
    )

    result = impl._plan_affected_files(task_dir)

    # Exact equality: the malformed bullet must be skipped entirely (fail-closed),
    # so only the well-formed bare-path bullet contributes.  Pre-change parsers that
    # used strip("`") would extract the residue "— reason after empty span" as a
    # spurious path, making result != frozenset({"docs/good.md"}) and failing here.
    assert result == frozenset({"docs/good.md"}), (
        f"empty backtick span must be skipped; only the well-formed bullet may be collected, got {result!r}"
    )
