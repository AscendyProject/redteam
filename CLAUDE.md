# redteam — agent-pair harness (standalone OSS)

This is the standalone home of the **redteam** harness: an adversarial agent-pair
workflow where one model writes code through a test-first pipeline and a second
model reviews it, gated at human checkpoints. It was extracted from a private
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

`v0.4.0` is released and the repo is public. Extraction, cross-stack validation,
Claude Code plugin packaging, and tier-aware routing (#13) are done. v0.2.0 added
the self-review guard (#28), `/redteam` commands + `review` subcommand (#29),
opt-in `--protect-config` (#30), and pipeline-mode validation (#36). v0.3.0 added
the reviewer fallback ladder (#37 step 4), install version-stamp + `--check`
(#34), the dispatch-time pre-implement snapshot invariant (#39), the operator
`progress.md` surface (#49), and a batch of fail-closed hardening (#40/#50/#51)
+ test-isolation fix (#54). v0.4.0 adds the task-scaffolding command (#55,
`orchestrator new` + `/redteam:redteam-new-task`), seeds a consumer's `verify.sh`
from a generic fail-closed template (#43), greens the cp949/Windows test suite
(#48), and records the #37/#67 reviewer-transport decisions — **both** step 6
(multiplexer transport) and step 5 (sub-agent reviewer adapter) rejected; see
`docs/decisions/`. See `CHANGELOG.md`.

**Roadmap:** no open issues. The reviewer-transport work (#37, umbrella) is fully
resolved — step 4 (fallback ladder) shipped in 0.3.0; steps 5 and 6 were rejected
as documented in `docs/decisions/2026-06-17-reviewer-transport-and-subagent.md`
(#67 closed). If a sub-agent reviewer is ever revived it restarts from a fresh
cross-provider `plan_review` and must clear the family-vs-key normalization
prerequisite first. Security-boundary changes go through `plan_review` when picked
up.

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
