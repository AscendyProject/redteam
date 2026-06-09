# FastAPI-like Backend — Sub-agent context

Compact reference loaded by every sub-agent. Authoritative source is `CLAUDE.md` at repo root —
this file mirrors only the parts a sub-agent needs to make safe code decisions.

## Domain
A media-processing / content backend. Handles upload sync, AI processing, and search over
user-submitted media. (Fictional, de-identified example — your real domain goes here.)

## Stack
- Python 3.11 + FastAPI + Pydantic + SQLAlchemy
- PostgreSQL (primary), Redis (cache + Celery broker)
- A vector database (similarity search), a search index (text/metadata)
- Celery workers (separate container)
- An object store (primary storage); a second object store (DR) is **planned, not wired up**
- A self-hosted GPU inference service for AI models:
  - An always-on inference server: detection + embedding models
  - A serverless runtime: an image-captioning model (has cold-start latency)
- An agent-orchestration library for the AI pipeline
- A managed Kubernetes cluster in prod; docker compose for local dev

## Architecture entry points
- `app/main.py` — FastAPI app factory
- `app/agents/` — 2-node pipeline (cognitive extraction + deterministic query-filter build)
- `app/services/sync/` — resumable, SHA-256 dedup, burst-aware
- `app/services/infrastructure/message_broker.py` — Protocol-based broker abstraction (Redis today)
- `app/services/infrastructure/object_store_service.py` — object-store client with hash-based reference counting
- `app/services/ai/` — captioning / embedding / reranking pipelines
- `app/schemas.py` — shared Pydantic types (single file, not a package)

## Hard rules (must respect when writing code)
- **Broker calls go through `Broker`.** Never import `redis`/a concrete broker client directly in business logic.
- **Vector-DB filter expressions come from deterministic Python**, never an LLM. Cognitive extraction returns
  Pydantic structured output → Python builds the filter expression.
- **Object-store keys are content-hash based** (SHA-256), not user-path based. Reference counting is the dedup mechanism.
- **Celery tasks must be idempotent** with explicit retry policy. New task without these is a bug.
- **AI model loading is split by serving runtime.** Always-on models stay on the inference server; the captioning
  model is serverless. Don't spin up parallel workers that reload models on either side.
- **Sync cursor commits BEFORE batch processing**, not after — avoids redundant AI preprocessing on resume.

## Architecture boundaries
- Route handlers must not call infra clients directly — go through services.
- Services must not bypass `Broker` for async work.
- Shared types live in `app/schemas.py`.
- Adding a new top-level dir under `app/` requires a CLAUDE.md update.

## Forbidden actions (sub-agents must refuse)
- `git push --force` to main or shared branches
- `rm -rf` outside `/tmp` or build artifacts
- Editing `.env*`, `secrets/`, or anything containing API keys
- `kubectl` against the production cluster
- Direct `psql`/`mysql` to production databases
- Deleting Alembic migrations from history
- Weakening tests or type checks to make CI pass

## Migrations
Alembic runs **inside the backend container**, not on the host:
- Local: `docker compose exec backend alembic upgrade head`
- Prod (Kubernetes): `kubectl exec <backend-pod> -- alembic upgrade head`

## Verification
Sub-agents that write code must run `bash .redteam/scripts/verify.sh` (ruff + ruff-format + mypy + pytest)
and report failures back to the orchestrator rather than papering over them.

## See also (do not load by default — too verbose for sub-agent context)
- `CLAUDE.md` — full collaboration rules
- `docs/architecture.md` — system diagram, data flow
- `docs/sync-design.md` — resumable sync, dedup semantics
- `docs/cost-model.md` — self-hosted vs API breakeven
