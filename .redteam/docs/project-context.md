# <Project name> — Sub-agent context

> **Template.** This is the compact reference every redteam sub-agent loads
> (path from `config.toml [project] context_file`). Fill it with your project's
> stack, entry points, and hard rules. Keep it short — it is mirrored into
> every agent prompt, so verbosity costs tokens on every phase. For a richer,
> real example see `examples/fastapi-like/.redteam/docs/project-context.md`.

The authoritative source is your repo's own docs (e.g. `CLAUDE.md`/`AGENTS.md`);
this file mirrors only what a sub-agent needs to make safe code decisions.

## Domain
One or two sentences: what the product is, what this codebase does.

## Stack
- Language / runtime / framework
- Datastores (primary DB, cache, queue, search/vector)
- External services (storage, model serving, third-party APIs)
- Deploy target (local dev + prod)

## Architecture entry points
- `path/to/main` — application entry / app factory
- `path/to/...` — the few modules a sub-agent must know to place a change correctly

## Hard rules (must respect when writing code)
List the invariants a change must never violate. Be concrete and checkable —
each line should let a reviewer say HIT/clean. Examples of the *kind* of rule:
- All async/IO goes through `<abstraction>`; never import `<low-level client>` in business logic.
- `<query/expr>` strings come from deterministic code, never from an LLM.
- `<background tasks>` must be idempotent with an explicit retry policy.

## Architecture boundaries
- Which layer may call which (e.g. routes → services → infra; never routes → infra).
- Where shared types live.
- What requires a docs update (new top-level module, public API shape change).

## Forbidden actions (sub-agents must refuse)
- `git push --force` to main or shared branches
- `rm -rf` outside `/tmp` or build artifacts
- Editing `.env*`, `secrets/`, or anything containing API keys/credentials
- Mutating production datastores or clusters directly
- Deleting migration history
- Weakening tests or type checks to make CI pass

## Migrations
How schema/data migrations are generated and applied (host vs container, local vs prod).

## Verification
Sub-agents that write code must run the project verify command
(`config.toml [project] verify_command`) and report failures back to the
orchestrator rather than papering over them.

## See also (do not load by default — too verbose for sub-agent context)
- Pointers to fuller design docs the sub-agent can open on demand.
