"""Standalone `orchestrator.py review` — one-shot cross-model review of the
current branch diff, no task/state machine.

Pins the command's contract:
- it resolves the configured reviewer adapter and runs it read-only on the diff;
- it prints the full review and persists it to .redteam/last_review.md;
- the exit code encodes the decision (0 APPROVED / 1 issues / 2 reviewer failed),
  fail-closed (a failed reviewer is never reported as an approval);
- a non-headless reviewer (reviewer="human") exits with guidance, not a crash.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


class _FrozenDatetime:
    """Stand-in for orchestrator.datetime with a fixed now(), so two runs collide
    on the same archive timestamp — #162's same-commit-twice case."""

    @staticmethod
    def now(tz=None):
        import datetime as _dt

        return _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)


def _load_orchestrator_module():
    import _engine

    return _engine.orchestrator()


def _fake_adapter(decision: str, parse_status: str = "ok", raw: str | None = None) -> MagicMock:
    fake = MagicMock()
    fake.review.return_value = {
        "decision": decision,
        "raw": raw if raw is not None else f"IR-001 ...\nREVIEW_DECISION: {decision}",
        "parse_status": parse_status,
    }
    return fake


def _result(decision: str, parse_status: str = "ok", raw: str | None = None) -> dict:
    return {
        "decision": decision,
        "raw": raw if raw is not None else f"IR-001 ...\nREVIEW_DECISION: {decision}",
        "parse_status": parse_status,
    }


def test_review_approved_returns_zero_and_saves(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    # get_reviewer_adapter is still used for the None check + self-review guard;
    # the actual review now flows through the fallback ladder (review_with_fallback).
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    rwf = MagicMock(return_value=_result("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", rwf)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "REVIEW_DECISION: APPROVED" in out  # full review printed
    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "REVIEW_DECISION: APPROVED" in saved  # persisted for reference
    # The ladder was asked for a read-only branch_diff review.
    _, kwargs = rwf.call_args
    assert kwargs["target"] == {"kind": "branch_diff", "base": "main"}
    assert kwargs["role"] == "review_code"


def test_review_changes_requested_returns_one(monkeypatch, tmp_path) -> None:
    """Issues found is a SUCCESSFUL review run, but a non-zero exit so it can gate CI."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("CHANGES_REQUESTED"))
    monkeypatch.setattr(orch, "review_with_fallback", lambda *a, **k: _result("CHANGES_REQUESTED"))
    assert orch.cmd_review(repo=tmp_path) == 1


def test_review_failed_reviewer_returns_two(monkeypatch, tmp_path) -> None:
    """Fail-closed: when the reviewer fails infra and the fallback ladder exhausts
    to manual (parse_status manual_required), `review` exits 2 — never an approval
    even if a stray body says APPROVED."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(
        orch,
        "review_with_fallback",
        lambda *a, **k: _result("MISSING", parse_status=orch.MANUAL_REQUIRED, raw="codex timed out; manual required"),
    )
    assert orch.cmd_review(repo=tmp_path) == 2


def test_review_without_headless_reviewer_exits_with_guidance(monkeypatch, tmp_path, capsys) -> None:
    orch = _load_orchestrator_module()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: None)
    rc = orch.cmd_review(repo=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "human" in err and "reviewer" in err  # actionable guidance, no crash


def test_review_refuses_same_provider_self_review(monkeypatch, tmp_path, capsys) -> None:
    """Fail-closed cross-provider guard AND a pin that cmd_review resolves providers
    through the shared worker_provider/reviewer_provider (not the adapter's own
    .name). The reviewer adapter is deliberately named "codex" while the resolvers
    report a "claude"/"claude" collapse: ONLY code that consults the resolvers
    refuses here. The pre-convergence implementation keyed off adapter.name /
    get_worker_adapter().name, so it would see "codex" vs "claude", NOT collapse,
    and run the reviewer — failing this test (so it pins the convergence)."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    reviewer = MagicMock()
    reviewer.name = "codex"  # adapter name disagrees with the resolver verdict on purpose
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: reviewer)
    monkeypatch.setattr(orch, "worker_provider", lambda state: "claude")
    monkeypatch.setattr(orch, "reviewer_provider", lambda state: "claude")  # collapse per the resolvers

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 2
    err = capsys.readouterr().err
    assert "self-review" in err  # actionable, names the collapse
    reviewer.review.assert_not_called()  # refused via the resolver path, before running the reviewer


def test_review_fails_closed_on_bad_config(monkeypatch, tmp_path, capsys) -> None:
    """#40: a malformed/unreadable .redteam/config.toml must exit 2 with guidance
    (fail-closed), not raise a traceback (exit 1), and never resolve/run a reviewer."""
    orch = _load_orchestrator_module()
    called = {"reviewer": False}

    def _boom(rr):
        raise ValueError("unknown key 'verfy_command' in [project]")

    def _reviewer(state):
        called["reviewer"] = True
        return MagicMock()

    monkeypatch.setattr(orch, "load_config", _boom)
    monkeypatch.setattr(orch, "get_reviewer_adapter", _reviewer)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()
    assert called["reviewer"] is False  # bailed before resolving the reviewer


def test_standalone_prompt_suspends_pipeline_artifact_gate() -> None:
    """#103: the standalone review runs with no task artifacts, so its prompt must
    explicitly suspend code_review.md's pipeline-only Required Checks
    (verification.log / state.json / outcome.md) — otherwise the reviewer fails
    closed on their absence and `review` can never return APPROVED. Pins that the
    prompt names those artifacts as not-required AND still demands a diff-level
    review (so it isn't softened into a rubber stamp)."""
    orch = _load_orchestrator_module()

    class _Proj:
        base_branch = "main"
        security_checklist = ".redteam/docs/security-checklist.md"
        context_file = ".redteam/docs/project-context.md"

    class _Cfg:
        project = _Proj()

    prompt = orch._standalone_review_prompt(_Cfg())
    low = prompt.lower()
    # the gate is explicitly suspended for the artifacts that don't exist standalone
    assert "verification.log" in prompt and "state.json" in prompt and "outcome.md" in prompt
    assert "do not require those" in low
    assert "do not emit changes_requested" in low
    # but it is still an adversarial diff review, not a waiver
    assert "security checklist" in low
    assert "pre-change" in low


def test_review_dispatched_by_main_without_batch(monkeypatch) -> None:
    """`review` takes no batch dir — main must route it without requiring argv[2]."""
    orch = _load_orchestrator_module()
    called = {"review": False}

    def _fake_review():
        called["review"] = True
        return 0

    monkeypatch.setattr(orch, "cmd_review", _fake_review)
    assert orch.main(["orchestrator.py", "review"]) == 0
    assert called["review"] is True


# ---------------------------------------------------------------------------
# #166 — the standalone verdict declares the conditions it was produced under
# ---------------------------------------------------------------------------


def test_standalone_review_declares_its_mode(monkeypatch, tmp_path) -> None:
    """#166: a standalone APPROVED asserts less than an in-pipeline one — no
    verification gate, no outcome.md alignment — so the artifact must say so.

    Emitted by the harness, not requested from the model: a reviewer that omitted
    the line would look exactly like a full pipeline review.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "abc1234" if ref == "HEAD" else "base9999")

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "MODE: standalone" in saved
    assert "NOT asserted" in saved
    assert "abc1234" in saved  # reviewed commit, so a verdict is tied to its input
    # The base endpoint is pinned to a SHA too: a branch name alone cannot
    # reconstruct `main...HEAD` once main moves.
    assert "base9999" in saved


def test_standalone_header_does_not_disturb_the_decision_parse(monkeypatch, tmp_path) -> None:
    """The header is present AND the artifact still parses last-line-wins.

    Asserted together on purpose: "exactly one REVIEW_DECISION line" is trivially
    true before this change (there was no header to add one), so it cannot
    discriminate alone. Paired with the header assertion — which is new — the whole
    fails against pre-change code while still pinning that the header must never
    introduce a decision line that would flip a later re-read.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("CHANGES_REQUESTED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("CHANGES_REQUESTED")))
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "abc1234")

    rc = orch.cmd_review(repo=tmp_path)

    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    from phase_runners._base import parse_review_decision

    assert rc == 1
    assert "MODE: standalone" in saved
    assert saved.count("REVIEW_DECISION:") == 1
    assert parse_review_decision(saved) == "CHANGES_REQUESTED"


def test_standalone_header_survives_unavailable_git(monkeypatch, tmp_path) -> None:
    """A repo where rev-parse fails still gets the mode line — the declaration is
    the point, the sha is a bonus, and losing git must not lose the warning."""
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))

    def _boom(ref, repo):
        raise RuntimeError("not a git repo")

    monkeypatch.setattr(orch, "git_rev_parse", _boom)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "MODE: standalone" in saved
    assert "reviewed: unknown" in saved


def test_standalone_header_pins_the_reviewed_commit_before_dispatch(monkeypatch, tmp_path) -> None:
    """#166 review IR-001: HEAD must be captured BEFORE the reviewer runs.

    Simulates the branch advancing while review_with_fallback is in flight. The
    verdict describes what the reviewer read, so the header must name the OLD sha
    and flag the move — resolving HEAD afterwards would attribute the approval to
    a commit that was never reviewed.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))

    # One HEAD resolution before dispatch, one after — the second yields a new commit.
    heads = iter(["before1", "after2"])

    def _rev(ref, repo):
        return next(heads) if ref == "HEAD" else "base9999"

    monkeypatch.setattr(orch, "git_rev_parse", _rev)

    def _slow_review(*a, **kw):
        # Stand-in for time passing: the next HEAD resolution yields a new commit.
        return _result("APPROVED")

    monkeypatch.setattr(orch, "review_with_fallback", _slow_review)

    rc = orch.cmd_review(repo=tmp_path)

    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert rc == 0
    assert "reviewed: before1" in saved, "must name the commit the reviewer actually read"
    assert "WARNING: HEAD moved" in saved
    assert "after2" in saved


def test_standalone_reviewer_is_given_the_pinned_sha_range(monkeypatch, tmp_path) -> None:
    """#166 review IR-001: the reviewer must be told to diff the SAME immutable
    range the header records.

    Naming branches in the prompt is a TOCTOU — either ref can move between our
    sampling and the reviewer actually running git — so the recorded range would
    not be the range read. Asserted at the diff-consumption boundary (the prompt
    and target handed to the ladder), not merely on mocked rev-parse values.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head111" if ref == "HEAD" else "base222")
    rwf = MagicMock(return_value=_result("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", rwf)

    orch.cmd_review(repo=tmp_path)

    _, kwargs = rwf.call_args
    assert "base222...head111" in kwargs["prompt"]
    assert "main...HEAD" not in kwargs["prompt"], "a movable ref must not define the reviewed range"
    assert kwargs["target"]["base"] == "base222"


def test_standalone_pins_each_endpoint_independently(monkeypatch, tmp_path) -> None:
    """#166 review IR-001: one endpoint failing must not un-pin the other.

    Realistic case — the configured base branch was renamed, so it cannot be
    resolved while HEAD can. An all-or-nothing fallback would revert the whole
    range to movable refs, letting HEAD advance before the reviewer reads it while
    the header still records the old head_sha.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))

    def _rev(ref, repo):
        if ref == "HEAD":
            return "head111"
        raise RuntimeError("no such branch")

    monkeypatch.setattr(orch, "git_rev_parse", _rev)
    rwf = MagicMock(return_value=_result("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", rwf)

    orch.cmd_review(repo=tmp_path)

    _, kwargs = rwf.call_args
    # The resolvable endpoint stays pinned; only the unresolvable one falls back.
    assert "main...head111" in kwargs["prompt"]
    assert "...HEAD" not in kwargs["prompt"]

    saved = (tmp_path / ".redteam" / "last_review.md").read_text(encoding="utf-8")
    assert "reviewed: head111" in saved
    assert "base: main (unknown)" in saved  # honest about the one that failed


# ---------------------------------------------------------------------------
# #162 — each review is archived, not overwritten
# ---------------------------------------------------------------------------


def test_standalone_review_is_archived_per_run(monkeypatch, tmp_path) -> None:
    """#162: a review must survive the next one.

    Overwriting a single last_review.md is what made run-to-run variance
    invisible — an operator sees one verdict and cannot tell it was one of
    several.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head1234" if ref == "HEAD" else "base9999")

    orch.cmd_review(repo=tmp_path)

    archived = list((tmp_path / ".redteam" / "reviews").glob("*.md"))
    assert len(archived) == 1
    assert "head1234"[:8] in archived[0].name, "the archive name ties the verdict to its input"
    assert "REVIEW_DECISION: APPROVED" in archived[0].read_text(encoding="utf-8")
    # The convenience copy still exists for existing habits/tooling.
    assert (tmp_path / ".redteam" / "last_review.md").exists()


def test_repeated_reviews_of_one_commit_are_kept_side_by_side(monkeypatch, tmp_path) -> None:
    """#162's central case: the SAME commit reviewed twice with different verdicts.

    Both must survive, or the variance the issue reports stays unobservable. A
    same-second collision on one sha is exactly this scenario, so it must
    de-duplicate rather than overwrite.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head1234" if ref == "HEAD" else "base9999")
    # Freeze the clock so both runs land on the same timestamp — the collision case.
    monkeypatch.setattr(orch, "datetime", _FrozenDatetime)

    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))
    orch.cmd_review(repo=tmp_path)

    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("CHANGES_REQUESTED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("CHANGES_REQUESTED")))
    orch.cmd_review(repo=tmp_path)

    bodies = [p.read_text(encoding="utf-8") for p in (tmp_path / ".redteam" / "reviews").glob("*.md")]
    assert len(bodies) == 2, "the first verdict must not be overwritten by the second"
    assert any("REVIEW_DECISION: APPROVED" in b for b in bodies)
    assert any("REVIEW_DECISION: CHANGES_REQUESTED" in b for b in bodies)


def test_archive_is_reported_and_its_failure_is_survivable(monkeypatch, tmp_path) -> None:
    """The archive path is surfaced to the operator, and losing it is not fatal.

    Both halves in one test on purpose: "a persistence failure still exits 0" was
    already true before this change (the single write was already wrapped), so it
    cannot discriminate alone. Paired with the reported-archive-path half — which
    is new — the whole fails against pre-change code while still pinning that the
    added mkdir/write cannot turn a completed review into a failure.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head1234")

    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err):
        rc_ok = orch.cmd_review(repo=tmp_path)
    assert rc_ok == 0
    assert ".redteam/reviews/" in err.getvalue(), "the operator must be told where the verdict was archived"

    # Now make every write fail: the verdict still stands and the exit code holds.
    def _boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(orch.Path, "mkdir", _boom)
    monkeypatch.setattr(orch.Path, "write_text", _boom)

    assert orch.cmd_review(repo=tmp_path) == 0


def test_archive_name_is_reserved_atomically_against_a_racing_process(monkeypatch, tmp_path) -> None:
    """#162 review IR-001: another process winning the race must not cost a verdict.

    exists()-then-write is a TOCTOU — two reviews of one commit finishing in the
    same second can both see the name as free, and the second write destroys the
    first. Simulated by making the first exclusive-create fail as if a competitor
    took the name between our attempts; the review must land in a DISTINCT file
    and the competitor's body must be untouched.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    reviews = tmp_path / ".redteam" / "reviews"
    reviews.mkdir(parents=True)
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head1234")
    monkeypatch.setattr(orch, "datetime", _FrozenDatetime)
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))

    # A competitor already holds the first name, exactly as an atomic reservation
    # by another process would leave it.
    taken = reviews / "20260101T000000Z-head1234.md"
    taken.write_text("COMPETITOR VERDICT\n", encoding="utf-8")

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0
    assert taken.read_text(encoding="utf-8") == "COMPETITOR VERDICT\n", "the other verdict must survive"
    others = [p for p in reviews.glob("*.md") if p != taken]
    assert len(others) == 1
    assert "REVIEW_DECISION: APPROVED" in others[0].read_text(encoding="utf-8")


def test_a_failed_write_leaves_no_truncated_archive(monkeypatch, tmp_path) -> None:
    """#162 review IR-001: a partial write must not survive at the archive name.

    The exclusive create can succeed and the WRITE still fail — disk or quota
    exhaustion — leaving a truncated file at the authoritative filename where it
    would later read as a completed audit record. A missing archive is honest; a
    half one is not. The earlier failure test only broke mkdir, so it never
    reached this path.
    """
    orch = _load_orchestrator_module()
    (tmp_path / ".redteam").mkdir()
    monkeypatch.setattr(orch, "git_rev_parse", lambda ref, repo: "head1234")
    monkeypatch.setattr(orch, "get_reviewer_adapter", lambda state: _fake_adapter("APPROVED"))
    monkeypatch.setattr(orch, "review_with_fallback", MagicMock(return_value=_result("APPROVED")))

    real_open = orch.Path.open

    class _PartialWriter:
        """Creates the file for real, then fails mid-write."""

        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[: len(data) // 2])  # a partial record lands on disk
            raise OSError("no space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def _open(self, mode="r", *a, **kw):
        fh = real_open(self, mode, *a, **kw)
        if "x" in mode and self.suffix == ".md":
            return _PartialWriter(fh)
        return fh

    monkeypatch.setattr(orch.Path, "open", _open)

    rc = orch.cmd_review(repo=tmp_path)

    assert rc == 0, "a persistence failure must not turn a completed review into a failure"
    leftovers = list((tmp_path / ".redteam" / "reviews").glob("*.md"))
    assert leftovers == [], f"a truncated archive survived: {leftovers}"

    # Contrast: with writes working, a run DOES archive. Asserted here because
    # "no .md files" is trivially true where no archive feature exists at all, so
    # the cleanup half cannot discriminate on its own.
    monkeypatch.setattr(orch.Path, "open", real_open)
    assert orch.cmd_review(repo=tmp_path) == 0
    assert len(list((tmp_path / ".redteam" / "reviews").glob("*.md"))) == 1
