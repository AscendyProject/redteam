"""Tests for the tightened "would have failed before" Required Check rule (#159 + #161).

Clause C eligibility for .redteam/prompts/codex/code_review.md is established by
the audit recorded in outcome.md: the file has exactly four in-repo consumers —
review_code.py:42, review_code.py:117, orchestrator.py:2133, orchestrator.py:452 —
all of which embed only the file's *path* as a string.  None of them open, read,
parse, or interpret the file's contents within harness code.  The markdown
assertions in this file therefore ride Clause C's per-artifact exemption under the
rewritten rule's own terms.

The built-prompt regression tests call the actual _code_review_prompt() and
_narrowed_code_review_prompt() functions and assert on their assembled string output.
Per the rewritten Clause B, a test that exercises the code path by calling an actual
function is *not preventive*, even if the invariant already held; such tests are
justified by showing they would detect an incorrect implementation.

The template section tests check for content added by this diff and exercise the
install.py _seed_file path (install.py:221-234) rather than reading the template
source text directly — the template has an in-repo execution path via the installer
and therefore does not qualify for Clause C's source-text exemption.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import phase_runners.review_code as _rc  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CODE_REVIEW_MD = _REPO_ROOT / ".redteam/prompts/codex/code_review.md"

# Load install.py via importlib (same pattern as test_install.py) so the template
# tests can exercise the _seed_file installer path rather than reading source text.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_install_spec = importlib.util.spec_from_file_location("redteam_install_whf", _SCRIPTS / "install.py")
assert _install_spec and _install_spec.loader
_install_mod = importlib.util.module_from_spec(_install_spec)
_install_spec.loader.exec_module(_install_mod)

_TD_PATH = Path("/tmp/batch/tasks/task-001")
_BASE = "main"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slice_between_headings(text: str, start_heading: str, end_heading: str) -> str:
    """Return text between start_heading line and end_heading line (both exclusive)."""
    start_idx = text.find(start_heading)
    assert start_idx != -1, f"Heading not found: {start_heading!r}"
    newline = text.find("\n", start_idx)
    start_content = newline + 1
    end_idx = text.find(end_heading, start_content)
    if end_idx == -1:
        return text[start_content:]
    return text[start_content:end_idx]


def _extract_new_check_paragraph(section: str) -> str:
    """Return the rewritten 'would have failed' paragraph.

    The paragraph begins at '- For any new test added' and ends at the next
    top-level bullet (a line starting with '- ' at column 0).
    """
    start_marker = "- For any new test added"
    start = section.find(start_marker)
    assert start != -1, f"Marker not found in Required Checks section: {start_marker!r}"
    rest = section[start:]
    result_lines: list[str] = []
    first_line_seen = False
    for line in rest.split("\n"):
        if not first_line_seen:
            result_lines.append(line)
            first_line_seen = True
            continue
        # Stop at the next top-level bullet (starts with "- " at column 0)
        if line.startswith("- "):
            break
        result_lines.append(line)
    return "\n".join(result_lines)


def _slice_section(text: str, heading: str) -> str:
    """Return body of section with given heading, up to next '## ' heading or EOF."""
    m = re.search(r"^" + re.escape(heading), text, re.MULTILINE)
    assert m is not None, f"Heading not found: {heading!r}"
    content_start = text.find("\n", m.start()) + 1
    next_m = re.search(r"^## ", text[content_start:], re.MULTILINE)
    if next_m is None:
        return text[content_start:]
    return text[content_start : content_start + next_m.start()]


def _extract_decision_vocab(text: str) -> set[str]:
    """Parse REVIEW_DECISION vocabulary from a prompt string.

    Returns the set of decision values by extracting the primary value from
    'REVIEW_DECISION: VALUE' patterns and alternatives from '(or X / Y / Z)'
    parentheticals that follow a REVIEW_DECISION mention.  Supports exact-set
    equality checks (no new values, no removals).
    """
    values: set[str] = set()
    # Capture the primary value: REVIEW_DECISION: VALUE
    for m in re.finditer(r"REVIEW_DECISION:\s*(\w+)", text):
        values.add(m.group(1))
    # Capture alternatives in the same sentence: VALUE` (or ALT1 / ALT2 / ...)
    for m in re.finditer(r"REVIEW_DECISION:\s*\w+[^(]*\(or\s+([\w\s/]+)\)", text):
        for alt in m.group(1).split("/"):
            alt = alt.strip()
            if alt:
                values.add(alt)
    return values


# ---------------------------------------------------------------------------
# Built-prompt regressions
# (Done-when items 4 and 5; exercises _code_review_prompt /
# _narrowed_code_review_prompt code paths — not source-text guards)
# ---------------------------------------------------------------------------


def test_code_review_prompt_names_criteria_file():
    """Done-when item 4 (regression): _code_review_prompt still names
    .redteam/prompts/codex/code_review.md as the criteria file the reviewer applies.
    Exercises the _code_review_prompt() code path directly; not a source-text guard.
    Would fail against an implementation that removed the path from the prompt builder.
    """
    p = _rc._code_review_prompt(_TD_PATH, _BASE)
    assert ".redteam/prompts/codex/code_review.md" in p


def test_code_review_prompt_names_all_decision_values():
    """Done-when item 4 (regression, #103): _code_review_prompt enumerates exactly
    the four REVIEW_DECISION values — APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED,
    ASK_USER; no new values, no removals.
    Exercises the _code_review_prompt() code path directly.
    Would fail if any decision value is added, removed, or renamed.
    """
    p = _rc._code_review_prompt(_TD_PATH, _BASE)
    expected = {"APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"}
    found = _extract_decision_vocab(p)
    assert found == expected, f"REVIEW_DECISION vocabulary mismatch: found={found!r}, expected={expected!r}"


def test_narrowed_code_review_prompt_names_criteria_file():
    """Done-when item 5 (regression): _narrowed_code_review_prompt still names
    .redteam/prompts/codex/code_review.md as the criteria file the reviewer applies.
    Exercises the _narrowed_code_review_prompt() code path directly.
    Would fail against an implementation that removed the path from the narrowed prompt builder.
    """
    p = _rc._narrowed_code_review_prompt(_TD_PATH, _BASE, "abc1234", [])
    assert ".redteam/prompts/codex/code_review.md" in p


def test_narrowed_code_review_prompt_names_all_decision_values():
    """Done-when item 5 (regression, #103): _narrowed_code_review_prompt enumerates
    exactly the four REVIEW_DECISION values — APPROVED, CHANGES_REQUESTED,
    RESCUE_REQUIRED, ASK_USER; no new values, no removals.
    Exercises the _narrowed_code_review_prompt() code path directly.
    Would fail if any decision value is added, removed, or renamed.
    """
    p = _rc._narrowed_code_review_prompt(_TD_PATH, _BASE, "abc1234", [])
    expected = {"APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"}
    found = _extract_decision_vocab(p)
    assert found == expected, f"REVIEW_DECISION vocabulary mismatch: found={found!r}, expected={expected!r}"


# ---------------------------------------------------------------------------
# Markdown semantic-clause assertions
# (Done-when item 1; Clause C exemption applies — see module docstring)
# All assertions are scoped to the isolated rewritten paragraph, not the whole file.
# ---------------------------------------------------------------------------


def test_clause_a_source_text_bypass_named():
    """Done-when item 1 / Clause A: the rewritten paragraph names the source-text
    bypass as a severity:major violation.
    Fails against pre-change code (the one-liner at line 52 contains none of these clauses).
    Clause C applies: code_review.md has no in-repo consumer that parses its contents.
    """
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    section = _slice_between_headings(text, "## Required Checks", "## Finding Format")
    para = _extract_new_check_paragraph(section)
    assert "source text" in para
    # bypass / execution path must co-occur with source text
    assert "bypass" in para or "execution path" in para
    assert "severity:major" in para


def test_clause_b_preventive_suites_named():
    """Done-when item 1 / Clause B: the rewritten paragraph names preventive suites
    and requires a broken fixture through the same code path in the same file,
    asserted to fail, disqualifying fixtures that fail for unrelated/contrived reasons.
    Fails against pre-change code (the one-liner at line 52 contains none of these clauses).
    Clause C applies: code_review.md has no in-repo consumer that parses its contents.
    """
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    section = _slice_between_headings(text, "## Required Checks", "## Finding Format")
    para = _extract_new_check_paragraph(section)
    assert "preventive" in para
    assert "fixture" in para
    assert "same code path" in para
    assert "same file" in para
    assert "asserted to fail" in para
    # explicit disqualification of unrelated / contrived failure
    assert "unrelated" in para or "contrived" in para


def test_clause_c_no_execution_path_exemption_named():
    """Done-when item 1 / Clause C: the rewritten paragraph names the per-artifact
    no-in-repo-execution-path exemption with all required obligations (eligibility
    per artifact, naming consumers, configuration/templates/manifests/workflow warning,
    semantic clauses only, execution path preferred).
    Fails against pre-change code (the one-liner at line 52 contains none of these clauses).
    Clause C applies: code_review.md has no in-repo consumer that parses its contents.
    """
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    section = _slice_between_headings(text, "## Required Checks", "## Finding Format")
    para = _extract_new_check_paragraph(section)
    # per-artifact scoping (never by file class / glob / directory / extension)
    assert "per artifact" in para
    # the exemption condition
    assert "no in-repo execution path" in para
    # obligation to establish eligibility by naming consumers
    assert "consumers" in para
    assert "parse" in para or "interpret" in para
    # warning that configuration / templates / manifests / workflow definitions may
    # still have behaviourally reachable semantics
    assert "configuration" in para.lower() or "templates" in para.lower()
    assert "manifests" in para.lower() or "workflow" in para.lower()
    assert "too weak" in para.lower() or "not importable" in para.lower()
    # execution path preferred
    assert "preferred" in para
    # semantic clauses only, not incidental wording / formatting / ordering / whitespace
    assert "semantic" in para
    assert "formatting" in para or "whitespace" in para or "ordering" in para


def test_required_check_forbids_project_owned_override():
    """Done-when item 1 (regression, #161): the rewritten paragraph explicitly forbids
    a project-owned file from overriding or weakening this Required Check.
    Fails against pre-change code (the one-liner at line 52 does not name this restriction).
    Clause C applies: code_review.md has no in-repo consumer that parses its contents.
    """
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    section = _slice_between_headings(text, "## Required Checks", "## Finding Format")
    para = _extract_new_check_paragraph(section)
    assert "project-owned" in para


def test_decision_vocabulary_at_tail_of_file():
    """Done-when item 2 (tail regression, #103): all four REVIEW_DECISION values are
    still enumerated at the tail of code_review.md so the file can return APPROVED.
    Scoped to the whole file (existence is sufficient — concern is that no implementation
    of this task accidentally removes the Decision block).
    Clause C applies: code_review.md has no in-repo consumer that parses its contents.
    """
    text = _CODE_REVIEW_MD.read_text(encoding="utf-8")
    for value in ("APPROVED", "CHANGES_REQUESTED", "RESCUE_REQUIRED", "ASK_USER"):
        assert f"REVIEW_DECISION: {value}" in text, f"REVIEW_DECISION: {value} missing from {_CODE_REVIEW_MD}"


# ---------------------------------------------------------------------------
# Template regression — exercises the install.py _seed_file path
# (Done-when item 3; fails against pre-change code — section is newly added)
# The template .redteam/templates/docs/test-conventions.md is registered as a
# project seed at install.py:156 and copied to the consumer via _seed_file
# (install.py:221-234).  Tests assert on the installed consumer document after
# exercising that path, not on the template source text directly.
# ---------------------------------------------------------------------------


def test_template_has_runtime_coverage_section(tmp_path: Path) -> None:
    """Done-when item 3 / regression: after the harness installer seeds the
    test-conventions template into a consumer project via install.py _seed_file
    (install.py:221-234), the installed consumer document contains a
    '## Runtime coverage' section (heading exactly).
    Exercises the install.py install() → _seed_file() path; asserts on the
    installed consumer document, not the template source text.
    Fails against pre-change code: the section did not exist in the template before this diff.
    """
    _install_mod.install(tmp_path, overwrite=False, dry=False)
    text = (tmp_path / ".redteam/docs/test-conventions.md").read_text(encoding="utf-8")
    assert "## Runtime coverage" in text


def test_template_runtime_coverage_section_body(tmp_path: Path) -> None:
    """Done-when item 3 / regression: the installed consumer document's
    '## Runtime coverage' section (seeded via install.py _seed_file, install.py:221-234)
    requires importing, mounting, or executing the thing under test, and calls out
    source-text guards as insufficient when an execution path exists.
    Scoped to the isolated body of the section (between '## Runtime coverage'
    and the next '## ' heading or EOF).
    Exercises the install.py install() → _seed_file() path; asserts on the installed
    consumer document, not the template source text.
    Fails against pre-change code: the section did not exist before this diff.
    """
    _install_mod.install(tmp_path, overwrite=False, dry=False)
    text = (tmp_path / ".redteam/docs/test-conventions.md").read_text(encoding="utf-8")
    body = _slice_section(text, "## Runtime coverage")
    # requires execution-path exercising (importing / mounting / executing)
    exec_terms = ("import", "mount", "execut", "load")
    assert any(t in body.lower() for t in exec_terms), (
        "Runtime coverage section must name importing, mounting, or executing"
    )
    # calls out source-text guards as insufficient when an execution path exists
    assert "source" in body.lower() or "text" in body.lower()
    assert "execution path" in body.lower() or "code path" in body.lower()
