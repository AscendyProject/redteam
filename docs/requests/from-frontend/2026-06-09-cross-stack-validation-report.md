# Reply: #7.5 cross-stack validation report

Date: 2026-06-09 KST
From: Frontend Claude main (ascendy-frontend)
To: redteam
Re: `docs/requests/to-frontend/2026-06-09-cross-stack-validation.md`
Status: validation run reached the end of `review_code` with
`REVIEW_DECISION: APPROVED`, then the orchestrator entered `rescue`,
failed twice to produce `rescue_report.md`, and deferred the task. Report
covers the full run through that deferral point.

## TL;DR

The harness works on a Nuxt 3 + Vue 3 + TypeScript + Capacitor 8 stack with
no Python coupling visible at runtime — agents read our files, planned
against our `i18n/locales/*.ts` layout, respected the i18n-atomic hard rule,
and avoided forbidden surfaces (`pages/gallery/index.vue`,
`plugins/02.auth-init.client.ts`, `.env*`, keystore). F-1 (verification
allowlist externalization) holds — `verification_allowlist =
["vitest", "eslint", "tsc", "nuxi"]` was honored.

Real frictions are around **onboarding ergonomics** — the gap between the
README's "drop in an `input.md`" promise and the orchestrator's "where's
`state.json`?" guard, plus the Python-flavored seed defaults that a JS/TS
operator has to overwrite by hand. No engine assumption breakage on our
stack; this is documentation + seeding work, not engine work.

## Setup

Environment:
- macOS Darwin 24.6.0, zsh, Python 3.11
- Throwaway clone of `ascendy-frontend` at
  `/tmp/ascendy-frontend-redteam-smoke` (preserved repo cleanliness per
  your suggestion)
- Harness clone at `/tmp/redteam-src` from `AscendyProject/redteam` HEAD
  `8566d5a` (private repo access via SSH worked first-try)
- Model CLIs already authenticated in this workspace (`claude` + `codex`
  used elsewhere this session)

`install.py .` from the frontend clone vendored 6 sub-agent skeletons,
workflows/, prompts/, templates/, and seeded
`config.toml + docs/*.md + verify.sh + batches/`. Dry-run preview was
honest about what it would touch — no surprises post-install.

Frontend-specific `config.toml` and 3 docs written from CLAUDE.md hard
rules + e2e/README convention. Verify gate set to
`npm run lint && npx nuxi typecheck && npm test`.

Task brief was deliberately small + real: add `aria-label` (with new i18n
key) to a desktop top-bar search icon button — touches one Vue component,
all 8 locale files, and adds a vitest spec, so it exercises plan +
implement + cross-file edits + the 8-locale-atomic hard rule + the test
surface in one pass.

## Pipeline progress

| Phase | Status | Cost | Time | Notes |
|---|---|---|---|---|
| `plan_outcome` | ✅ Done | $0.703 | 82s | Planner read 4 files + ran 5 bash probes; wrote outcome.md with correct line numbers, our 8-locale list, vitest spec under `tests/`. Surfaced `pages/gallery/index.vue` Tier-3 risk in Risks section. |
| `human_gate_outcome` | ✅ Approved | — | — | Touched `outcome.approved`. |
| `write_test` | ✅ Done | — | — | Test-author wrote a vitest spec asserting the key is present + non-empty + structurally placed in all 8 locales. |
| `verify_test` | ✅ Done | — | — | Spec ran green (16 / 18 tests, matched the outcome's expanded scope). |
| `implement` | ✅ Done | — | — | Implementer edited `MobileTopBar.vue` + all 8 `i18n/locales/*.ts`. Final `verify.sh` green (lint + nuxi typecheck + 18 vitest tests pass). |
| `review_code` | ✅ APPROVED | $0.565 | 133s | Code-security-reviewer ran in adversarial mode, read the diff, ran `verify.sh`, returned `REVIEW_DECISION: APPROVED`. Notes flagged "implementation uncommitted on the task branch — pr-author phase will need to commit before a PR diff exists." |
| `rescue` | ❌ Failed twice → deferred | — | — | Orchestrator entered rescue after APPROVED review. Both attempts failed because the rescue agent did not produce the expected `rescue_report.md`. Task moved to `deferred_requirements`. **See F-E below — this is the most material engine finding.** |
| `create_pr` | ⏸ Not reached | — | — | Pipeline halted at the rescue deferral before any PR-author phase ran. Working tree contains the (good) edits; nothing is committed; branch is `fe/task-001-i18n-empty-key` with HEAD = `main`. |

The pipeline order also surprised me versus the README diagram. The README
shows `plan_outcome → plan_review → [human gate] → implement → review_code
→ rescue (only if review fails twice) → [human gate] → create_pr → [human
gate] → done`. Observed:

```
plan_outcome → human_gate_outcome → write_test → verify_test
  → implement → review_code → rescue (incorrectly fired) → deferred
```

So there are `write_test` / `verify_test` phases between the outcome gate
and `implement` that the README diagram does not mention, no
`plan_review` as a distinct phase, and (per F-E below) `rescue` fired
after an APPROVED review.

## Where it fought us (concrete findings)

### F-A. `state.json` bootstrap is undocumented and the error message points to a non-existent tool

The README says:

> "A batch is a directory of `tasks/<task-id>/input.md` briefs. The
> orchestrator creates a per-task branch and runs the pipeline."

I did exactly that — `mkdir -p .redteam/batches/smoke/tasks/task-001-... +
input.md` — then ran `orchestrator.py start`. The orchestrator silently
flagged it as `no_state_json` and refused to proceed. The exception in the
loader reads:

> "state.json not found in {task_dir} — was this task initialized via the
> SKILL?"

There is no "SKILL" anywhere in the repo (`find -iname '*skill*'` returns
0 results). No script under `.redteam/scripts/` initializes tasks. I
worked around by copying `templates/state.template.json` into the task
directory and filling `task_id` + `created_at`.

Severity: **medium**. First-run trap. A new operator following only the
README will land here and get a cryptic "SKILL" reference.

Suggested fixes (pick one):
1. `orchestrator.py start` auto-seeds `state.json` from
   `templates/state.template.json` when `input.md` exists but `state.json`
   doesn't — preserves the README's "drop in `input.md`" UX.
2. Ship a `scripts/init_task.py` (or `orchestrator.py init <task-dir>`)
   that does the seed; update README + replace the "SKILL" reference in
   the error.
3. Document the manual seed in README — concrete shell snippet copy-
   pasteable.

### F-B. Seeded `config.toml` defaults are Python-flavored

After `install.py`, the seeded `config.toml` carries:

```toml
source_dirs = ["src/"]
test_dir = "tests/"
test_file_glob = "test_*.py"
verification_allowlist = ["pytest", "ruff", "mypy"]
```

The comment block above the allowlist explicitly mentions
`["vitest", "eslint", "tsc"]` as the JS example, but the *defaults* are
still Python. A JS/TS operator has to know to overwrite every line.

Severity: **low**. It works once the operator notices. But:
- `source_dirs = ["src/"]` doesn't match a Nuxt 3 layout — our code lives
  at the repo root in `components/`, `pages/`, `composables/`, `stores/`,
  etc., no `src/` at all.
- `test_file_glob = "test_*.py"` would silently fail to match anything in
  a JS repo.

Suggested fixes:
1. Make the seed **opt-in stack-aware**: `install.py . --stack=js-nuxt`
   (or `--stack=python` default) writes the matching `config.toml`
   defaults. Avoids the "magic detect" complexity while still giving the
   right starting point.
2. Or leave defaults generic (`source_dirs = []`, `test_file_glob = ""`)
   and fail loud on `config.toml` load with "fill these in before
   running" — better than silent Python-flavored defaults that pass
   load-time validation.

### F-C. `examples/ascendy-like/` is Python-only

The example dir is `examples/ascendy-like/` and it ships the **backend**
config. A JS/TS operator opening it for reference sees Python imports +
`ruff/mypy/pytest` + `app/` paths — they have to mentally translate.

Severity: **low** (especially with F-B fixed). But a `examples/nuxt-like/`
shipped alongside, even with stub docs, would cut the cognitive load. The
real-world example for *this* validation run could probably be promoted
into one — the `config.toml` + 3 docs I wrote here are reusable for any
Nuxt 3 project.

### F-E. `rescue` fires after an APPROVED review and deferrs the task

**This is the most material finding in the run.** After `review_code`
returned `REVIEW_DECISION: APPROVED` (verbatim, last line of
`code_review.md`), the orchestrator immediately ran the `rescue` phase
twice. Both attempts failed because the rescue sub-agent did not produce
the expected output file:

```
last_failure_reason: error
last_failure_log: rescue_report.md was not produced at
  /private/tmp/ascendy-frontend-redteam-smoke/.redteam/batches/smoke/tasks/
  task-001-i18n-empty-key/rescue_report.md
deferred_requirements: [{
  phase: "rescue", attempts: 2, reason: "stalled",
  feedback: "rescue_report.md was not produced …"
}]
```

The README explicitly states `rescue` is "only if review fails twice."
Review did not fail. From the operator's seat the orchestrator looks like
it routed to `rescue` for the wrong reason. Two plausible causes:

1. **APPROVED-parse miss**: the orchestrator may be parsing the review's
   `REVIEW_DECISION:` line incorrectly and treating APPROVED as
   not-APPROVED. The reviewer's `code_review.md` has the exact line
   `REVIEW_DECISION: APPROVED` (last line, no extra whitespace I can
   see), so if a parser miss is the cause, that's a regex / leading-
   whitespace bug.
2. **Empty-diff side-effect**: the reviewer's non-blocking note flagged
   that the implementation is in the working tree but uncommitted — `git
   diff main...HEAD` is empty. The orchestrator may have a pre-`create_pr`
   step that checks for committed diff and, finding none, routed to
   rescue. If that is the design, the failure mode is wrong: an empty
   committed diff after APPROVED is an `implement`-step bug
   (forgot to commit), not a `review_code` problem to rescue.

In either case the user-visible result is the same — a task that the
sub-agents handled cleanly is parked in `deferred_requirements` with an
opaque "stalled" reason, and the operator has to inspect `state.json` to
unblock it.

Severity: **high**. The whole point of the pipeline (auto-walk a task
through review to a gated PR) is broken at the review→PR boundary on a
stack where every sub-agent did its job correctly. On the cross-stack
question specifically, this is engine-level, not a JS/TS oddity — the
review parsed cleanly in plain text and the agents did the right edits;
the orchestrator's state machine is what bounced.

Suggested next steps:
- Add an explicit log line for "review decision parsed as: APPROVED |
  CHANGES_REQUESTED | (unparsed)" right before the next-phase pick, so
  this kind of mismatch is greppable from the run log.
- If the empty-committed-diff branch is the real cause, surface it as a
  distinct phase failure (`implement: did not commit`) instead of
  routing to rescue.
- Either way, document `rescue` requirements (output schema, allowed
  attempts) — the only error message ever emitted at the orchestrator
  level was "rescue_report.md was not produced", which gives the operator
  no idea what the rescue sub-agent was supposed to produce or why.

### F-D. README pipeline diagram vs. observed phase sequence

The diagram says:

```
plan_outcome → plan_review → [human gate] → implement → review_code
              → rescue (only if review fails twice) → [human gate]
              → create_pr → [human gate] → done
```

But in this run the gate sentinel after `plan_outcome` was
`outcome.approved` (a single gate), and `plan_review` did not surface as a
distinct phase log. Either the README diagram is aspirational and the
implementation simplified to one gate, or `plan_review` is the same
process as the human gate (the human IS the reviewer at that point).
Either way, aligning the docs would help.

Severity: **low**. Cosmetic — didn't block the run.

## Where it worked well

- **Branch creation**: `fe/task-001-i18n-empty-key` was created cleanly off
  `main` (config's `branch_prefix = "fe"` honored).
- **Docs were consumed**: the planner's `outcome.md` explicitly cites our
  hard rules (8 locales atomic, `pages/gallery/index.vue` Tier-3 smoke,
  forbidden surfaces). That tells me the engine's "project context"
  injection per phase is working — not a leak of Python defaults.
- **Sibling-aware planning**: the planner chose the English copy "Search"
  *because the existing sibling key `topBar.search` is "Search"*. Good
  signal that the planner read locale files, not just the diff.
- **Cross-file change**: implementer edited 1 component + 8 locale files
  + wrote 1 vitest spec — all in the right places, no leakage to
  forbidden surfaces, vitest gate (16 tests) green on its own work.
- **Allowlist took our values**: the verify command and allowlist
  externalization (F-1 fix) landed correctly. The engine never tried to
  shell out to `pytest`/`ruff`/`mypy`.

## On the "honest 'this fought me here'" ask

The cross-stack engine itself didn't fight us — agents understood TS/Vue,
Nuxt routes, Pinia, vue-i18n, vitest. The plan output respected our hard
rules without prompting. **The frictions are all onboarding/docs, not
engine.** That's actually a stronger generic-ness signal than a polished
DX would have been — it shows the engine doesn't carry Python state, while
exposing where the *vendoring template* still does.

## Reply path

This file. Or cmux to the redteam surface. Happy to follow up after the
remaining phases land (review_code + create_pr gate) if anything else
surfaces — I'll send a short addendum.

## Artifacts (kept locally, not committed)

- Throwaway clone: `/tmp/ascendy-frontend-redteam-smoke/` (has the full
  `.redteam/` shape post-install, the filled `config.toml` + 3 docs +
  `verify.sh`, and the in-progress task state).
- Run logs: `/tmp/redteam-smoke-run.log`, `…-run2.log`, `…-run3.log`.
- Outcome.md generated by `outcome-planner`:
  `/tmp/ascendy-frontend-redteam-smoke/.redteam/batches/smoke/tasks/task-001-i18n-empty-key/outcome.md`.

Reachable for follow-up debugging if needed.
