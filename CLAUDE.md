# redteam — agent-pair harness (standalone OSS)

This is the standalone home of the **redteam** harness: an adversarial agent-pair
workflow where one model writes code (plan → implement; tests written inside
`implement`) and a second model reviews it adversarially; the draft PR is the
human checkpoint before merge. (A single-model test-first **TDD** mode — `write_test → verify_test`
before `implement` — is also available.) It was extracted from a private
monorepo — where it was built as that project's internal harness — into this
open-source repo (`AscendyProject/redteam`, Apache-2.0), which owns it going
**forward**.

## What this repo is

- **Engine** (`.redteam/workflows/`): `orchestrator.py` + `phase_runners/` +
  `adapters/` + `config.py`. Stdlib-only, zero runtime deps.
- **Prompts** (`.redteam/prompts/codex/`), **agent skeletons** (`.claude/agents/`,
  6 generic sub-agents), **templates** (`.redteam/templates/`).
- **Project-owned, dogfood config** (`.redteam/config.toml`, `.redteam/docs/*`,
  `.redteam/scripts/verify.sh`): these describe THIS repo — redteam dogfoods its
  own harness. `examples/fastapi-like/` is a real, richer (Python) example.
- **Installer** (`.redteam/scripts/install.py`): vendors the harness into a
  consumer repo (copy model, not pip — the engine resolves repo root from its own
  file location, so it must live inside the consumer's `.redteam/`).
- **Packaging**: `LICENSE` (Apache-2.0 verbatim), `CLA.md`, `README.md`, `pyproject.toml`.

## Commands

```bash
# Verify (this repo's own gate — ruff + pytest over .redteam/):
bash .redteam/scripts/verify.sh
# or directly:
ruff check .redteam/ && pytest .redteam/tests -q

# Dogfood the harness on itself (drive a real task through the pipeline):
python3 .redteam/workflows/orchestrator.py start  .redteam/batches/<batch>
python3 .redteam/workflows/orchestrator.py resume .redteam/batches/<batch>
python3 .redteam/workflows/orchestrator.py status .redteam/batches/<batch>

# Validate the installer into a throwaway target:
python3 .redteam/scripts/install.py /tmp/some-repo --dry-run
```

A Python venv with `ruff` + `pytest` is required for the tests (a local `venv/`
is auto-activated by `verify.sh` if present).

## How to develop this repo

Two modes, pick by size:

- **Direct edit** for trivial fixes (typos, a one-liner, a doc tweak). Edit,
  `bash .redteam/scripts/verify.sh`, commit.
- **Dogfood** for real features: write a task `input.md` under
  `.redteam/batches/<batch>/tasks/<task-id>/`, run the orchestrator, and let the
  harness drive itself (Claude implementer + Codex reviewer) through the pipeline.
  This is the truest ongoing validation — the harness developing the harness.

Either way, **security-boundary or multi-file changes go through Codex review**
before merge (mirrors the agent-pair discipline this project embodies). The
verification allowlist, the installer's file-class split (harness-owned vs
project-owned), the snapshot/fail-closed logic, and the adapter trust model are
all security boundaries — never loosen them inline; plan_review first.

## Hard rules

- **Engine stays project-agnostic.** No project- or stack-specific fingerprints
  in `.redteam/workflows/` or non-example tests. Project specifics live in
  `.redteam/config.toml` + `.redteam/docs/*` (project-owned) or under
  `examples/`. `test_agents_generic_prompts.py` guards agent bodies; keep it green.
- **Zero runtime dependencies.** The engine imports only the stdlib. Adding a pip
  dependency is a deliberate, reviewed decision (it breaks the "vendor + run"
  promise).
- **Installer must never delete consumer-owned files.** Harness-owned trees live
  entirely under `.redteam/` (safe to replace); agent skeletons are copied
  file-by-file (a consumer's own `.claude/agents/*` must survive `--overwrite`);
  project-owned files (`config.toml`, `docs/*`, `verify.sh`, `batches/`) are
  seeded once and never overwritten. Regression-tested in `test_install.py` —
  keep those invariants.
- **LICENSE is Apache-2.0; contributions are under `CLA.md`.** Don't change the
  license or weaken the CLA without the operator's explicit decision.
- **No force-push to `main`; no committing secrets.** Standard.

## Project status

`v0.6.0` is released and the repo is public. Extraction, cross-stack validation,
Claude Code plugin packaging, and tier-aware routing (#13) are done. v0.2.0 added
the self-review guard (#28), `/redteam` commands + `review` subcommand (#29),
opt-in `--protect-config` (#30), and pipeline-mode validation (#36). v0.3.0 added
the reviewer fallback ladder (#37 step 4), install version-stamp + `--check`
(#34), the dispatch-time pre-implement snapshot invariant (#39), the operator
`progress.md` surface (#49), and a batch of fail-closed hardening (#40/#50/#51)
+ test-isolation fix (#54). v0.4.0 added the task-scaffolding command (#55,
`orchestrator new` + `/redteam:new-task`), seeded a consumer's `verify.sh`
from a generic fail-closed template (#43), greened the cp949/Windows test suite
(#48), and recorded the #37/#67 reviewer-transport decisions — **both** step 6
(multiplexer transport) and step 5 (sub-agent reviewer adapter) rejected; see
`docs/decisions/`. v0.5.0 makes the default common path **gateless** (#71/#75 —
removes `human_gate_outcome` from the static orders, opt-in per tier; the draft PR
is the human checkpoint) and realigns the agent-pair/TDD docs so the default flow
is no longer mislabeled "test-first" (#72/#77). v0.5.1 fixes the subagent tool
restriction (#76 — the skeletons used the ignored `allowed-tools:` key instead of
`tools:`, so per-agent tool limits were silently dropped). v0.6.0 lands **goal
mode** (#94) — `orchestrator decompose` turns a human `goal.md` into a
single-parent DAG manifest (`goal.json`) via a decomposer sub-agent + cross-provider
decomposition review, then runs the tasks parent-first with each dependent **stacked
on its parent's branch** (reviewed range / PR base / changed-paths pinned to
`parent-branch...HEAD`), fail-closed throughout (multi-parent rejected in v1,
`ceilings.max_tasks` mismatch aborts the batch, moved-parent-tip freeze guard,
descendants of a non-done parent are `blocked_on_dependency`); design in
`docs/decisions/2026-06-27-goal-mode-design.md` (#110), engine in #111/#115,
e2e composition tests in #123/#126. v0.6.0 also closes the implement
commit-boundary integrity family (#91 base-branch pin A+B, #112 set-once untracked
baseline, #117 cross-run trust-root floor, #124 sibling-task floor exemption, #82
commit discipline), adds a deterministic per-role model picker + `config` subcommand
(#95) and an anti-degeneracy review check (#97), plus worker/review robustness
(#109/#87/#99/#103/#119/#86). See `CHANGELOG.md`.

`v0.7.0` reworks the Claude Code plugin command surface to match the engine
(#130): the slash commands drop the redundant `redteam-` prefix
(`/redteam:install`, not `/redteam:redteam-install` — **breaking**), and three
commands that were CLI-only are now exposed (`/redteam:goal`, `/redteam:start`,
`/redteam:resume`) so the plugin can run/resume/goal-decompose a batch, not just
scaffold one; the picker now maps ~1:1 to the orchestrator subcommands. v0.7.0 is
the first release cut by the raidostar-gated release automation (#128). It also
adds the goal-mode operator docs to the README (EN+KO), closing the doc gap
under #94.

Post-0.7.0 (unreleased): `/redteam:goal` was reworked into an **autonomous
run-to-completion driver** with a goal-aware machine-readable `status --json`
(#134), and the **first autonomous goal run** then shipped #92's remaining
proposals as a stacked pair — P3 round-staged reviewer (#135) and P5 hard
review-loop ceilings (#140; review trail in #139) — closing #92. That run also
surfaced three engine gaps, filed as #136 (goal-mode decompose seeds vs the
#117/#91 floors, incl. a #117↔sweep-baseline catch-22), #137 (pre-worker floor
ignores plan-declared Affected files → self-lock on review backtrack), and
#138 (planner keeps emitting a non-parseable `## Verification hooks` section).

**Roadmap:** goal mode v1 engine + e2e + operator docs are shipped and #92 is
closed; remaining open work is the native-diff coupling follow-up (#120), the
pure-visual-task fit question (#132), plan-aware review (#133), and the
autonomous-run hardening trio #136/#137/#138 (floor exemptions are
security boundaries — plan_review first). Goal mode v1 is a **single-parent
forest** — multi-parent (a task depending on ≥2 others) fails closed and is future
work; if revived it restarts from a fresh `plan_review`. The reviewer-transport work
(#37, umbrella) is fully resolved — step 4 (fallback ladder) shipped in 0.3.0; steps
5 and 6 were rejected as documented in
`docs/decisions/2026-06-17-reviewer-transport-and-subagent.md` (#67 closed).
Security-boundary changes go through `plan_review` when picked up.

Coordination with downstream adopters of the harness is tracked **privately**,
outside this public repo. For project work here, use GitHub issues / PRs /
discussions.

## Blog intake (standing order)

The Ascendy blog team sources posts from project agents. redteam adopts the OSS
variant of their standing order (`ascendy-blog/docs/intake-standing-order-oss.md`).
Operationally:

- **When.** Once per cycle in which a release, a merge, or a decision landed, drop
  one blog-intake. No real material that cycle → a one-line `urgency: backlog`
  note. Never manufacture an angle. Pure chores (dep bumps, typos) don't count.
- **Where — NOT this repo.** Raw intake goes to the blog repo's gitignored path
  `ascendy-blog/docs/requests/from-redteam/YYYY-MM-DD-<kebab-topic>.md` — never
  into this public repo. A pre-redaction raw committed to a public repo is exposed
  permanently in git history (force-push can't fully erase it). That drop path is
  in the blog repo's `.gitignore` (verified), so a normal `git add` won't commit
  it (a forced `git add -f` still would — don't).
- **Format.** Copy `ascendy-blog/docs/intake-template.md` verbatim. `team:
  redteam`; `suggestedCategory` is usually `meta` (project/pattern posts).
- **Canon honesty — the public repo is the source of truth.** Before writing any
  fact, verify it against the real repo with `gh`: license, version, issue# vs
  PR#, OPEN vs CLOSED, **shipped vs roadmap**. Precedent to avoid: a 0.3.0 intake
  once read as if `#37` were "implemented" when it was an OPEN issue (only the
  fallback-ladder step had shipped). When unsure, mark "검증 필요" in the body for
  the blog team's pre-publish fact-check rather than asserting.
- **Special trigger — actively drop Claude↔Codex debates.** When the pair diverged
  substantively for 3+ rounds and then converged (or honestly forked), that is the
  highest-value material. Write the *tension itself* (each side steelmanned, the
  crux of the split, the convergence path) — not a "we agreed" summary. Use the
  template's debate body structure.
- **Sensitive content** that creeps in (unreleased business decisions, customer
  identifiers, un-remediated security gaps) goes in the "공유하면 안 되는 부분"
  section — flagged, not hidden, so the blog team can redact.

The blog team pulls from the drop path on its own cadence; ping their cmux surface
only for `urgency: urgent`. Intake ≠ publication.

## AGENTS.md

`AGENTS.md` is Codex's guide for reviewing/working in this repo (the adversarial
half of the pair). Keep the two in sync when conventions change.
