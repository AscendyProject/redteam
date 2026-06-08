# Ascendy Backend — Sub-agent context

Compact reference loaded by every sub-agent. Authoritative source is `CLAUDE.md` at repo root —
this file mirrors only the parts a sub-agent needs to make safe code decisions.

## Domain
Closed-social photo platform for families and couples. Backend handles sync, AI processing, and search.

## Stack
- Python 3.11 + FastAPI + Pydantic + SQLAlchemy
- PostgreSQL (primary), Redis (cache + Celery broker)
- Milvus Standalone (vector search), Elasticsearch (text/metadata)
- Celery workers (separate container)
- Cloudflare R2 (primary storage); Backblaze B2 (DR) is **planned, not wired up**
- Self-hosted AI on RunPod (4 models total):
  - Triton on always-on L4 pod: SCRFD (TensorRT), BuffaloL (TensorRT), Qwen3-VL-Embedding-2B
  - vLLM serverless: Qwen3-VL-Instruct-2B (image captioning has cold-start latency)
- LangGraph for agent orchestration
- Vultr VKE (Kubernetes) in prod; docker compose for local dev

## Architecture entry points
- `app/main.py` — FastAPI app factory
- `app/agents/` — LangGraph 2-node (cognitive extraction + deterministic Milvus expr)
- `app/services/sync/` — resumable, SHA-256 dedup, burst-aware
- `app/services/infrastructure/message_broker.py` — Protocol-based broker abstraction (Redis today)
- `app/services/infrastructure/s3_service.py` — R2 client with hash-based reference counting
- `app/services/ai/` — captioning / embedding / reranking pipelines
- `app/schemas.py` — shared Pydantic types (single file, not a package)

## Hard rules (must respect when writing code)
- **Broker calls go through `BrokerProtocol`.** Never import `redis`/`aiokafka` directly in business logic.
- **Milvus `expr` strings come from deterministic Python**, never an LLM. Cognitive extraction returns
  Pydantic structured output → Python builds the expr.
- **S3 keys are content-hash based** (SHA-256), not user-path based. Reference counting is the dedup mechanism.
- **Celery tasks must be idempotent** with explicit retry policy. New task without these is a bug.
- **AI model loading is split by serving runtime.** Triton stays on the L4 pod; vLLM is serverless.
  Don't spin up parallel workers that reload models on either side.
- **Sync cursor commits BEFORE batch processing**, not after — avoids redundant AI preprocessing on resume.

## Architecture boundaries
- Route handlers must not call infra clients directly — go through services.
- Services must not bypass `BrokerProtocol` for async work.
- Shared types live in `app/schemas.py`.
- Adding a new top-level dir under `app/` requires a CLAUDE.md update.

## Forbidden actions (sub-agents must refuse)
- `git push --force` to main or shared branches
- `rm -rf` outside `/tmp` or build artifacts
- Editing `.env*`, `secrets/`, or anything containing API keys
- `kubectl` against the production VKE cluster
- Direct `psql`/`mysql` to production databases
- Deleting Alembic migrations from history
- Weakening tests or type checks to make CI pass

## Migrations
Alembic runs **inside the backend container**, not on the host:
- Local: `docker compose exec backend alembic upgrade head`
- Prod (VKE): `kubectl exec <backend-pod> -n ascendy -- alembic upgrade head`

## Verification
Sub-agents that write code must run `bash .redteam/scripts/verify.sh` (ruff + ruff-format + mypy + pytest)
and report failures back to the orchestrator rather than papering over them.

## See also (do not load by default — too verbose for sub-agent context)
- `CLAUDE.md` — full collaboration rules
- `docs/architecture.md` — system diagram, data flow
- `docs/sync-design.md` — resumable sync, dedup semantics
- `docs/cost-model.md` — self-hosted vs API breakeven
