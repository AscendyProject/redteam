# redteam — agent-pair harness (standalone OSS)

This is the standalone home of the **redteam** harness: an adversarial agent-pair
workflow where one model writes code through a test-first pipeline and a second
model reviews it, gated at human checkpoints. It was extracted from the private
`ascendy-backend` monorepo's `.redteam/` and now lives as its own project
(`AscendyProject/redteam`, private, AGPLv3). The extraction *history* stays in
ascendy-backend (`docs/ideation/harness/agentic-pair/`); this repo owns the
project going **forward**.

## What this repo is

- **Engine** (`.redteam/workflows/`): `orchestrator.py` + `phase_runners/` +
  `adapters/` + `config.py`. Stdlib-only, zero runtime deps.
- **Prompts** (`.redteam/prompts/codex/`), **agent skeletons** (`.claude/agents/`,
  6 generic sub-agents), **templates** (`.redteam/templates/`).
- **Project-owned, dogfood config** (`.redteam/config.toml`, `.redteam/docs/*`,
  `.redteam/scripts/verify.sh`): these describe THIS repo — redteam dogfoods its
  own harness. `examples/ascendy-like/` is a real, richer (Python) example.
- **Installer** (`.redteam/scripts/install.py`): vendors the harness into a
  consumer repo (copy model, not pip — the engine resolves repo root from its own
  file location, so it must live inside the consumer's `.redteam/`).
- **Packaging**: `LICENSE` (AGPLv3 verbatim), `CLA.md`, `README.md`, `pyproject.toml`.

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

A Python venv with `ruff` + `pytest` is required for the tests. (The backend
repo's venv works; a local `venv/` is auto-activated by `verify.sh` if present.)

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

- **Engine stays project-agnostic.** No ascendy/Python fingerprints in
  `.redteam/workflows/` or non-example tests. Project specifics live in
  `.redteam/config.toml` + `.redteam/docs/*` (project-owned) or in
  `examples/ascendy-like/`. `test_agents_generic_prompts.py` guards agent bodies;
  keep it green.
- **Zero runtime dependencies.** The engine imports only the stdlib. Adding a pip
  dependency is a deliberate, reviewed decision (it breaks the "vendor + run"
  promise).
- **Installer must never delete consumer-owned files.** Harness-owned trees live
  entirely under `.redteam/` (safe to replace); agent skeletons are copied
  file-by-file (a consumer's own `.claude/agents/*` must survive `--overwrite`);
  project-owned files (`config.toml`, `docs/*`, `verify.sh`, `batches/`) are
  seeded once and never overwritten. Regression-tested in `test_install.py` —
  keep those invariants.
- **LICENSE is AGPLv3; contributions are under `CLA.md`.** Don't change the
  license or weaken the CLA without the operator's explicit decision.
- **No force-push to `main`; no committing secrets.** Standard.

## Cross-repo coordination (this session owns it)

redteam is now a first-class sibling alongside `ascendy-backend`,
`ascendy-frontend`, `ascendy-infra` (top-level), `ascendy-blog`. When the harness
needs another team to act (e.g. cross-stack validation, #7.5), THIS session writes
the handoff — backend no longer coordinates on redteam's behalf.

```text
# Outgoing — write into the recipient repo's intake, then cmux-notify:
ascendy-frontend/docs/requests/from-redteam/<YYYY-MM-DD>-<topic>.md
ascendy-backend/docs/requests/from-redteam/<YYYY-MM-DD>-<topic>.md
# (infra: ~/Documents/ascendy/docs/agent-os/requests/from-redteam/... )

# Incoming replies land here:
docs/requests/from-frontend/<YYYY-MM-DD>-<topic>.md
docs/requests/from-backend/<YYYY-MM-DD>-<topic>.md
```

Staged drafts awaiting dispatch live under `docs/requests/to-<sibling>/`.
There is a staged frontend #7.5 handoff at
`docs/requests/to-frontend/2026-06-09-cross-stack-validation.md` — dispatching it
is this session's first coordination task. cmux safety: place text in the target
surface with `cmux send`, then STOP before any Enter unless the operator confirms.

## Project status / next steps

Extraction #1–#7 done; F-1 fixed. **#7.5 and #8 are DONE; `v0.1.0` is tagged.**

- **#7.5 cross-stack validation — DONE (2026-06-09).** Frontend (Nuxt/Vue/TS) ran a
  real task end-to-end; the engine is generic on JS/TS with no Python coupling at
  runtime. All five findings fixed: F-E (HIGH — TDD-mode approved review wrongly
  routed to rescue; PR #5), F-A (MEDIUM — auto-seed state.json + drop stale "SKILL"
  ref + default mode → agent-pair; PR #6), F-B/C/D (LOW — fail-loud empty seed
  config, `examples/nuxt-like/`, README mode note; PR #7). Infra stays deferred
  (TDD-poor Helm/yaml stack — separate "non-TDD support" question).
- **#8 Claude Code plugin packaging — DONE.** Repo doubles as a single-plugin
  marketplace (`.claude-plugin/{plugin,marketplace}.json` + `bin/redteam-install`
  + `commands/redteam-install.md`), Option A (vendored-copy unchanged; PR #3). Live
  `claude --plugin-dir .` load + `claude plugin validate .` pass. Pre-tag/release
  checklist: re-run `claude plugin validate .` (not in CI — no `claude` there).
- **Next: flip the repo public**, then the backend re-install PR (below).
- **Drift decision (DECIDED 2026-06-09): this repo is the single source of
  truth; backend re-installs as a consumer, deferred until this repo is
  release-tagged.** `ascendy-backend/.redteam/` is the extraction origin (last
  touched at backend #170 = this repo's b1a8ce4 baseline; no divergent backend
  work) and now lags by F-1 + generic defaults + packaging — all behaviorally
  inert for backend (it overrides via its real config.toml and is a Python
  stack). So the drift is harmless until then. When this repo cuts `v0.1.0`
  (after #7.5 + #8), backend opens a PR: `install.py <backend> --overwrite` from
  the tag (refreshes the engine; backend's ascendy config.toml/docs/verify.sh/
  batches are project-owned and preserved), `git rm` the three dead scripts
  (doctor.py, install-claude.sh, redteam_status.py — stale pre-`workflows/`
  scaffolding), and add `verification_allowlist` to backend's config.toml. That
  PR doubles as install.py `--overwrite` validation on a real Python consumer.
  Do NOT re-install before the tag (chasing `main` defeats reproducibility).

See `docs/cross-stack-findings.md` for the #7.5 smoke results and F-1.

## AGENTS.md

`AGENTS.md` is Codex's guide for reviewing/working in this repo (the adversarial
half of the pair). Keep the two in sync when conventions change.
