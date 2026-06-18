# redteam — Sub-agent context

> The compact reference every redteam sub-agent loads (path from
> `config.toml [project] context_file`). This repo **dogfoods its own harness**,
> so this file describes redteam itself. The authoritative, fuller sources are
> `CLAUDE.md` (Claude guide) and `AGENTS.md` (reviewer guide); this file mirrors
> only what a sub-agent needs to make safe code decisions. Keep it short — it is
> mirrored into every agent prompt.

## Domain
redteam is an **adversarial agent-pair harness**: one model writes code
(`plan → implement`, tests written inside `implement`) and a second,
*different-provider* model reviews it adversarially; the draft PR is the human
checkpoint before merge. (A single-model test-first **TDD** mode — `write_test → verify_test` before
`implement` — is also available.) This repo is the standalone OSS home of that
harness and runs the harness on itself.

## Stack
- **Python 3, standard library only — zero runtime dependencies.** Adding a pip
  dependency to the engine breaks the "vendor + run" promise and is a reviewed
  decision, not a casual one.
- A CLI orchestrator, not a service: **no database, no web server, no network
  listener, no untrusted user-input surface.** The trust boundaries are the
  *agents* it drives and the *consumer repos* it installs into.
- Dev tooling: `ruff` + `pytest` (in a local venv), driven by
  `.redteam/scripts/verify.sh`.

## Architecture entry points
- `.redteam/workflows/orchestrator.py` — the state machine: `start` / `resume` /
  `status` / `review`, `process_task`, phase routing, human gates.
- `.redteam/workflows/phase_runners/` — one runner per phase; `_base.py` holds
  the verification allowlist + `run_claude` + git/diff helpers.
- `.redteam/workflows/adapters/` — worker vs reviewer adapters (codex/claude) and
  the provider resolvers (`worker_provider` / `reviewer_provider`).
- `.redteam/workflows/config.py` + `.redteam/config.toml` — the config seam; the
  single place project specifics live.
- `.redteam/scripts/install.py` — vendors the harness into a consumer repo.

## Hard rules (must respect when writing code)
- **Engine stays project-agnostic.** No project- or stack-specific fingerprints
  in `.redteam/workflows/` or non-example tests. Project specifics live in
  `config.toml` + `.redteam/docs/*` or under `examples/`.
- **Zero runtime dependencies.** The engine imports only the stdlib.
- **Installer never deletes consumer-owned files.** Harness-owned trees (under
  `.redteam/`) are replaceable; agent skeletons are copied file-by-file; project-
  owned files (`config.toml`, `docs/*`, `verify.sh`, `batches/`) are seeded once
  and never overwritten.
- **Adversarial pair must stay cross-provider.** The reviewer must resolve to a
  different provider than the worker; same-provider review is self-review and is
  refused fail-closed.
- **Subprocess calls are shell-free** (arg lists, never `shell=True`) and pin
  `encoding="utf-8"` on any text-mode capture.
- **LICENSE is Apache-2.0; contributions under `CLA.md`.** Don't change the
  license direction or weaken the CLA without the operator's explicit decision.

## Architecture boundaries
- Runners ask the adapter registry which adapter owns a role; they never hardcode
  a provider. Project specifics flow through `config.py`, never inline in runners.
- The reviewer adapter is **read-only**; only the worker adapter mutates the
  workspace.

## Forbidden actions (sub-agents must refuse)
- `git push --force` to `main` or shared branches.
- `rm -rf` outside `/tmp` or build artifacts; deleting consumer-owned files in a
  target repo.
- Editing `.env*`, `secrets/`, or anything containing API keys/credentials.
- Adding a non-stdlib import to the engine without explicit justification.
- Weakening tests, types, or lint to make CI pass.

## Verification
Sub-agents that write code must run the project verify command
(`config.toml [project] verify_command` → `bash .redteam/scripts/verify.sh`,
i.e. `ruff` + `pytest` over `.redteam/`) and report failures back to the
orchestrator rather than papering over them.

## See also (do not load by default)
- `AGENTS.md` — "What matters most in THIS repo (security boundaries)".
- `CLAUDE.md` — full project guide and hard rules.
- `examples/fastapi-like/.redteam/docs/` — a web/DB-shaped example of these docs.
