# Test conventions — FastAPI-like backend

Facts pulled from `tests/conftest.py` and `tests/api/`. If a sub-agent updates the test
infrastructure, refresh this file at the same time.

## Layout
- Single conftest at `tests/conftest.py` (no per-package conftest yet).
- API tests live under `tests/api/test_*.py`. There is currently no `tests/unit/`,
  `tests/integration/`, or `tests/services/` split — everything goes through the FastAPI
  TestClient.
- pytest config lives in `[tool.pytest.ini_options]` in `pyproject.toml`. No separate
  `pytest.ini`.

## How external systems are stubbed
All swap-outs happen at **module import time** in `conftest.py`, before the FastAPI app is
imported. Sub-agents writing new tests should not re-mock these — the autouse fixtures and
`sys.modules` patches already cover them.

- The GPU inference-service client library → `MagicMock` via `sys.modules`
- `qrcode`, `pyotp` → `MagicMock` via `sys.modules`
- `redis`, `redis.asyncio` → `MagicMock` via `sys.modules`. The `mock_redis` fixture
  (`autouse=True`) additionally patches `app.redis_client.get_redis` to return a shared
  `mock_redis_obj`.
- The vector-DB client library → `MagicMock` via `sys.modules`.
  The vector-DB connection is never opened in tests.
- `celery`, `celery.app`, `celery.schedules`, `celery.result` → `MagicMock` via `sys.modules`.
  **There is no Celery eager mode** — tasks are not executed in tests, they are mocked
  callable shells. If you need to assert "task was enqueued", patch the task's `delay` /
  `apply_async`.
- `app.ws_manager._start_pubsub_listener` is patched to no-op so app lifespan doesn't try
  to connect to redis pubsub.

## Database
- Engine is in-memory SQLite (`sqlite:///:memory:`) with `StaticPool`.
- PostgreSQL types are aliased so models load: `postgresql.JSONB = JSON`,
  `postgresql.ARRAY = JSON`. Don't write raw `JSONB` operator queries (`@>`, `->>`) in
  code paths exercised by tests — they won't run on SQLite.
- `init_db` (session-scope, autouse) creates and drops `Base.metadata` once per session.
- `db_session` (function-scope) wraps each test in a transaction that's rolled back at
  teardown — so test isolation is per-function.

## Core fixtures
- `db_session: Session` — transactional, rolled back at teardown.
- `client: TestClient` — FastAPI TestClient with `get_db` overridden to use `db_session`.
- `auth_client: TestClient` — `client` with a JWT for `test_user` set in `Authorization`.
- `test_user`, `test_user_2` — pre-verified users (`is_email_verified=True`,
  `password_changed_at` backdated by 1 hour to bypass force-rotation logic).
- `test_storage` — a `local`-provider Storage row owned by `test_user`.
- `mock_object_store` — patches `delete_object`, `upload_fileobj`,
  `generate_presigned_url` on `app.services.infrastructure.object_store_service`.
- `mock_background_tasks` (autouse) — patches `fastapi.BackgroundTasks.add_task` so
  background work doesn't run during tests.

## Async / concurrency
- No `pytest-asyncio` configuration is present. Tests use the synchronous
  `fastapi.testclient.TestClient` and don't `await` anything directly.
- If you need to test an `async def` service function, the current convention is
  unestablished — call it via the route that exercises it, or wrap with
  `asyncio.run(...)` inside the test. Prefer the route-level path.

## Environment variables set at test load
`DATABASE_URL=sqlite:///:memory:`, `SECRET_KEY=test-secret-key`,
`INTERNAL_EVENT_TOKEN=test-token`, `RECAPTCHA_SECRET_KEY=` (empty),
`AWS_ACCESS_KEY_ID=testing`, `AWS_SECRET_ACCESS_KEY=testing`. Don't override these in
individual tests unless the test explicitly needs to exercise misconfigured behavior.

## Gaps the sub-agent should NOT silently fill
- No fixture for vector-DB query results — patch `app.services.ai.vector_store_service`
  callsites directly when you need a return shape.
- No fixture for the search index — the relevant client is not yet stubbed centrally.
- No fixture for inference responses — see `app.services.ai.inference_client`.
- No coverage / mutation-testing config in `pyproject.toml`.

If a needed fixture is missing, add it to `tests/conftest.py` and update this file in the
same PR rather than inlining ad-hoc setup.
