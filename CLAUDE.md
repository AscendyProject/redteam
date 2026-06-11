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

`v0.1.0` is released and the repo is public. Extraction, the cross-stack
validation that proved the engine generic on a non-Python stack, and Claude Code
plugin packaging are all done.

**Roadmap:** tier-aware routing — let a task's risk tier select its phases and
models instead of one uniform pipeline (issue #13). It's a security-boundary
change, so it goes through `plan_review` when picked up.

Coordination with downstream adopters of the harness is tracked **privately**,
outside this public repo. For project work here, use GitHub issues / PRs /
discussions.

## AGENTS.md

`AGENTS.md` is Codex's guide for reviewing/working in this repo (the adversarial
half of the pair). Keep the two in sync when conventions change.
