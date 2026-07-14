# Outcome — fix `_plan_affected_files` bullet→path parser for the standard `` - `path` — reason `` form (#149)

## Goal
Make `_plan_affected_files` in `.redteam/workflows/phase_runners/implement.py`
extract the correct single repo-relative path from the standard
`` - `path/to/file` — one-line reason `` bullet emitted by the outcome-planner
template, so that a plan-declared out-of-scope file is actually exempted by the
#137 pre-worker floor — without ever widening exemption to more than that one
declared path.

## Done-when
- [ ] `_plan_affected_files` parses `` - `.redteam/templates/x.json` — one-line reason ``
      to `frozenset({".redteam/templates/x.json"})` (exact string equality —
      no trailing backtick, no ` — reason` residue, no directory prefix).
- [ ] A new regression test in `.redteam/tests/test_floor_plan_affected_files_exemption.py`
      asserts the standard `` - `path` — reason `` form with an out-of-scope path
      is present in the returned set (positive case for #149).
- [ ] A new adversarial regression test in the same file asserts that a
      malformed / comment-laden bullet yields either the correct single path or
      an empty result — never a broad path, never a directory prefix, never a
      second path per bullet.
- [ ] Existing behaviour preserved and asserted still-green by the pre-existing
      cases in `test_floor_plan_affected_files_exemption.py`:
      bare `- path`, `- (new) path` (case-insensitive, positional, single
      occurrence), absolute-path skip, `..`-segment skip, `\` → `/`
      normalization, missing/unreadable `outcome.md` → empty frozenset,
      no-heading → empty frozenset, stop-at-same-or-higher-heading boundary,
      set-once snapshot semantics, cross-run trust-root floor NOT exempted.
- [ ] A bare `- path/to/file` bullet with no description still parses to
      `path/to/file` (no separator required).
- [ ] The description separator handling accepts both ` — ` (em dash) and
      ` - ` (hyphen) when the path is bare (no backticks).
- [ ] `bash .redteam/scripts/verify.sh` (ruff + pytest over `.redteam/`) exits 0.
- [ ] No code outside `.redteam/workflows/phase_runners/implement.py` and
      `.redteam/tests/test_floor_plan_affected_files_exemption.py` is modified.
- [ ] Inside `implement.py`, only `_plan_affected_files`'s per-bullet
      path-extraction logic changes; `_floor_outside_scope`,
      `_cross_run_trust_root_floor`, `_is_harness_artifact`,
      `_get_or_set_plan_affected_files_baseline`, the commit/integrity gate,
      and every guard listed in the input brief (absolute skip, `..` skip,
      empty skip, `\` → `/`, fail-closed empty frozenset, stop-at-heading)
      remain byte-identical in behaviour.
- [ ] The engine adds no non-stdlib import (`re` is already imported).

## Out of scope
- Any change to `_floor_outside_scope`, `_cross_run_trust_root_floor`,
  `_is_harness_artifact`, `_get_or_set_plan_affected_files_baseline`, or the
  commit/integrity gate.
- Any change to `.redteam/templates/outcome.template.md` (the template is
  already correct — the parser is what must accept it).
- Any change to the outcome-planner skeleton or its prompt.
- Any new config, CLI flag, or telemetry surface.
- Any change to files outside `.redteam/workflows/phase_runners/implement.py`
  and `.redteam/tests/test_floor_plan_affected_files_exemption.py`.
- Support for multiple paths per bullet, glob patterns in bullets, or
  directory-tree exemption — one bullet still yields at most one exact path.

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — replace the two-line
  strip-based extraction inside `_plan_affected_files` (lines ~243–244) with
  logic that: if the bullet body (after the leading `-`/`*` and single
  positional `(new) ` prefix) starts with a backtick, extract the content of
  the **first** `` `...` `` span and discard any trailing residue; otherwise
  take the text up to the first ` — ` or ` - ` description separator (or the
  whole line if no separator).
- `.redteam/tests/test_floor_plan_affected_files_exemption.py` — add the
  standard-form positive regression and the adversarial no-over-exemption
  regression; keep every existing case intact.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- New tests under `.redteam/tests/test_floor_plan_affected_files_exemption.py`
  covering:
  - Positive: standard `` - `path` — reason `` bullet with an out-of-scope
    path parses to that exact path (present in returned frozenset).
  - Positive: standard `` - `(new) path` — reason `` bullet with the `(new) `
    prefix inside/outside the backticks parses to that exact path.
  - Positive: bare `- path/to/file` (no description, no backticks) still
    parses to `path/to/file`.
  - Positive: bare `- path/to/file - reason` (hyphen separator) parses to
    `path/to/file`.
  - Adversarial: a malformed bullet whose shape could tempt an over-broad
    extraction (e.g. embedded whitespace/backticks, empty backtick span,
    directory-looking token) MUST yield either the correct single path or
    an empty result — never a directory prefix, never a second path,
    never a path that would exempt an unrelated tree.
  - Regression: every existing case listed in the file's module docstring
    (bare, `(new) `, absolute skip, `..` skip, `\` → `/`, missing file,
    no heading, heading boundary, set-once snapshot, cross-run trust-root
    NOT exempted, default byte-identical for in-scope-only tasks) remains
    green.

## Risks
- **Description-separator ambiguity.** The template uses ` — ` (em dash), but
  humans (and prior tasks) frequently substitute ` - ` (hyphen). The plan
  accepts both for the bare-path form; a stricter em-dash-only rule would
  narrow exemption but also silently drop legitimate hyphen bullets. Chosen
  narrowest-that-works: accept both, backtick-quoted path is the recommended
  form, no glob semantics ever.
- **Adversarial-shape enumeration.** "Never a directory prefix, never a
  second path" is the security-boundary invariant; the adversarial test in
  the To-be-created list encodes it against one concrete malformed shape.
  If the implementer's regex admits a distinct malformed shape that yields
  a broad path, it's a defect against this outcome and must be surfaced,
  not delegated. (Per the input brief's operator-delegation clause,
  security-boundary widening is explicitly NOT delegated.)
- **Backward-compat for `- (new) `\`path`\`` ordering.** The current code
  strips a leading backtick pair, then `(new) `, then trailing backticks.
  Real-world outcome.md's have been observed with `(new) ` both inside and
  outside the backtick span. The plan preserves both orderings; if the
  implementer finds a canonical form baked into other tests, prefer that
  and record the choice.
