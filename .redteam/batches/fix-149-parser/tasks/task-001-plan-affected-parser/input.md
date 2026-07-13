# Task: fix `_plan_affected_files` bullet→path parser for the standard `- `path` — reason` form (#149)

## Context

`_plan_affected_files` in `.redteam/workflows/phase_runners/implement.py` parses
the `## Affected files` section of a task's `outcome.md` into the set of paths
that the pre-worker floor exempts (#137 — plan-declared out-of-scope files).
It currently only strips leading/trailing backtick runs:

```python
raw = bm.group(1).strip().strip("`").strip()
raw = new_prefix_re.sub("", raw).strip().strip("`").strip()
```

For the **standard** bullet emitted by `.redteam/templates/outcome.template.md`
and the outcome-planner skeleton —

```markdown
- `path/to/file` — one-line reason
```

— `.strip("`")` removes the leading backtick but the line ends in prose (not a
backtick), so the closing backtick + ` — reason` survive. The parsed "path"
becomes `` path/to/file` — one-line reason `` and never matches the real git
path. Result: a legitimately-declared **out-of-scope** file is *not* exempted
and the floor fail-closes (the false positive that self-blocked the #146 Phase 0
goal run). In-scope files are unaffected — they pass via the scope roots
regardless — so the bug only bites out-of-scope plan-declared files (docs,
config templates), which is exactly the case #137 was designed to allow.

## Required behavior (Done-when)

For each bullet under `## Affected files` (levels `#` / `##` / `###`, as today),
after stripping a single positional `(new) ` prefix (case-insensitive, as
today):

- If the remaining text begins with a backtick, the path is the content of the
  **first** `` `...` `` span; any trailing ` — reason` / ` - reason` /
  `` `reason` `` after the closing backtick is ignored.
- Otherwise (bare path, no backticks), the path is the text up to the first
  description separator (` — ` or ` - `) — a bare `- path/to/file` with no
  description still parses to `path/to/file`.
- Preserve every existing guard **exactly**:
  - absolute paths (`/…`) skipped,
  - any `..` path segment skipped,
  - empty entries skipped,
  - `\` → `/` normalization,
  - fail-closed empty `frozenset` when `outcome.md` is absent/unreadable or has
    no Affected-files heading,
  - stop-at-same-or-higher-heading boundary.

## Hard constraints — SECURITY BOUNDARY

- The parser feeds the floor **EXEMPTION**. It must never widen exemption
  beyond the exact path token in a bullet. A malformed, comment-laden, or
  adversarial bullet must yield either the correct single path or **nothing** —
  never a broad or surprising path, never more than one path per bullet, never a
  directory prefix that would exempt a whole tree. When in doubt, extract
  nothing (fail-closed).
- Do **NOT** touch `_floor_outside_scope`, `_cross_run_trust_root_floor`,
  `_is_harness_artifact`, the set-once snapshot mechanism
  (`_get_or_set_plan_affected_files_baseline`), or the commit/integrity gate.
  Only the path-extraction inside `_plan_affected_files` changes.
- Engine stays **stdlib-only** (`re` is already imported) and project-agnostic.
- Backward compatible: the two forms already handled (bare `- path`,
  `- (new) path`) must still parse identically.

## Files in scope

- `.redteam/workflows/phase_runners/implement.py` — modify only
  `_plan_affected_files`'s per-bullet path extraction.
- `.redteam/tests/test_floor_plan_affected_files_exemption.py` — add regression
  coverage; keep every existing case green.

Do **not** modify anything outside `.redteam/workflows/` or `.redteam/tests/`.
Do not reformat `.redteam/templates/outcome.template.md` (the template is fine;
the parser is what must accept the standard form).

## Regression coverage (must be included)

- The standard `` - `path` — reason `` form with an **out-of-scope** path
  (e.g. `` - `.redteam/templates/x.json` — one-line reason ``) asserted to be
  exempted (i.e. present in the returned set).
- An **adversarial** bullet asserting NO over-exemption — e.g. a bullet whose
  malformed shape must NOT yield a broad path (empty result, or a scoped safe
  path, but never a directory prefix or a second path).
- Existing cases (bare `- path`, `- (new) path`, absolute-path skip,
  `..`-segment skip, `\` → `/`, missing/unreadable `outcome.md`, no heading,
  stop-at-same-or-higher heading) remain green.

## Non-goals

- No change to any floor predicate, the snapshot mechanism, or the integrity
  gate — only `_plan_affected_files`'s bullet→path extraction.
- No new config, CLI, or telemetry surface.
- No reformatting of `outcome.template.md`.

## Operator delegation (autonomy clause)

Plan-level scope questions are delegated to the operator agent: prefer the
narrowest parser change that satisfies the required behavior above, and record
any such decision in `ask_user_response.md` (or the final report).
**Security-boundary widening is NOT delegated** — if a choice could make the
parser exempt more than the single declared path, stop and surface it rather
than deciding.

## Verification

Run the project verify command (`bash .redteam/scripts/verify.sh`, i.e.
`ruff` + `pytest` over `.redteam/`) and report failures back to the orchestrator
rather than papering over them. The plan's `## Verification` section must be a
fenced ```yaml block with a `commands:` list containing that command, so the
runner parses it.
