# Security checklist — FastAPI-like backend

The code-security-reviewer agent applies this list to every diff. Each item below is a
**hard line** — a single confirmed HIT must escalate to `REVIEW_DECISION: CHANGES_REQUESTED`.
Don't relax the checklist for "small" changes; small diffs are where regressions hide.

## 1. Injection
- [ ] **SQL injection.** No raw SQL strings built with f-strings / `%` / `.format()`
      where any interpolated value originates from user input or any DB column the user
      can control. SQLAlchemy `text()` calls must use bound parameters, not concatenation.
- [ ] **Vector-DB expression injection.** Vector-database filter expressions must be
      built by deterministic Python from validated structured input — never by an LLM,
      never by string-concatenating user input. If the diff lets free-form text reach a
      filter expression, that's a HIT regardless of how "harmless" the values look.
- [ ] **Search-index query injection.** No user input dropped into a raw query-string or
      scripted-scoring source. Use the index's parameterized query DSL.
- [ ] **Shell / subprocess injection.** Any `subprocess.run` / `os.system` with
      `shell=True` and user input is a HIT. Pass `args=[...]` lists and `shell=False`.

## 2. Path / file handling
- [ ] **Path traversal.** User-supplied filenames or keys must be normalized and
      checked to be inside the allowed prefix before being joined to a base path.
      Reject `..`, absolute paths, and NUL bytes.
- [ ] **Object-store keys.** Object keys are content-hash-based (SHA-256). Do not concatenate
      user-controlled strings into keys; this both breaks dedup and creates a traversal
      surface across user prefixes.

## 3. Secrets and logs
- [ ] **No secrets in logs or error responses.** Tracebacks returned to clients must not
      include connection strings, JWTs, API keys, presigned URLs, or PII.
- [ ] **No secrets in commits.** `.env*`, `secrets/`, certificate files, or anything
      with API keys must not appear in the diff.
- [ ] **No PII in Celery task arguments.** Task arguments are serialized into Redis;
      pass IDs and have the worker hydrate from the DB rather than passing raw user
      objects, emails, or tokens.

## 4. AuthN / AuthZ
- [ ] **JWT / session validation.** Every protected route depends on the auth
      dependency — no route bypasses it via a custom decorator or in-route check.
- [ ] **Per-user authorization on every read/write.** Routes that take an `id` param
      (`media_id`, `album_id`, `group_id`, …) must verify the resource belongs to the
      caller (or to a group the caller is a member of). "Authenticated" alone is not
      "authorized."
- [ ] **No credential rotation bypass.** Don't introduce code paths that let
      `password_changed_at` be unset or backdated outside the verified flow.

## 5. URL signing & external surfaces
- [ ] **Object-store presigned URL TTL.** Default TTL must be short (≤ 15 min for upload,
      ≤ 1 hour for download). No 7-day URLs unless the diff has a written reason.
- [ ] **Webhook / external callback signatures.** Inbound callbacks (payment providers,
      GPU-service job completions, etc.) must verify the provider's signature — don't trust
      the body.
- [ ] **Open redirect.** Any `RedirectResponse(url=user_input)` must validate the
      target host against an allowlist.

## 6. Concurrency / idempotency
- [ ] **Celery task idempotency.** New Celery tasks must have explicit `autoretry_for`,
      `retry_backoff`, `max_retries`, and a deterministic effect when re-run with the
      same arguments. Lacking any of these is a HIT.
- [ ] **Race conditions on shared writes.** Counters, reference counts, sync cursors
      must use DB-level atomic ops (`UPDATE … SET x = x + 1`) or row locks, not
      read-modify-write in Python.
- [ ] **Sync cursor commit order.** Cursor commits **before** batch processing, not
      after — anything that flips this order is a HIT.

## 7. Architecture invariants
- [ ] **No `Broker` bypass.** Business logic must not import `redis` or another concrete
      broker client directly. The only legal direct imports live in
      `app/services/infrastructure/message_broker.py` and the test conftest mocks.
- [ ] **No infra calls from route handlers.** Routes call services; services call infra.
      A route directly opening an object-store client or vector-DB connection is a HIT.
- [ ] **No new top-level dir under `app/` without CLAUDE.md update.** Architectural
      surface changes need to be documented.

## 8. AI model output trust boundary
Treat every model response (image captions, cognitive-extraction structured output,
reranker scores, OCR text, anything coming back from the inference service) as **untrusted
input**, on equal footing with raw user input. The fact that we generated the prompt does
not make the response safe — uploaded images can carry adversarial text, and prompt
injection has shipped in production at every shop that ignored this.

- [ ] **Length cap before persistence.** Every model output written to PostgreSQL,
      the vector DB, or the search index must pass through an explicit length check
      (column-aligned truncation or rejection). No "model said it, so it fits" assumptions.
- [ ] **HTML / Markdown escape on render paths.** If a model output ever reaches a
      response body or templated string, it must be HTML-escaped (or rendered through a
      safe-mode markdown renderer with raw-HTML disabled). Don't store
      already-escaped content — escape at the boundary where it's emitted.
- [ ] **Prompt-injection pattern sanitization.** Strip or neutralize patterns the model
      may have echoed back from a hostile image / document: `<script>` and other HTML
      tags in caption text, fenced code that looks like new instructions, attempts at
      system-prompt leakage (`"You are now…"`, `"ignore previous instructions"`,
      role-tag fragments). Reject silently rather than trying to "fix" a hit.
- [ ] **No model output flows back into another LLM call unfiltered.** A caption from
      the captioning model must not be concatenated into a downstream prompt without
      sanitization — that's how injection chains across stages.
- [ ] **No model output reaches a vector-DB filter expr, SQL, shell, or filesystem paths.**
      Same rule as user input (sections 1, 2): structured fields only, never string
      interpolation into a query/command/path.

## 9. Tooling
- [ ] **bandit clean.** Run `bandit -r <changed_files>` — any HIGH severity finding is
      a HIT. MEDIUM findings must be acknowledged in the review with an explicit reason
      to ignore.
- [ ] **No weakening of tests / mypy / ruff.** Diffs that add `# type: ignore`,
      `# noqa`, `xfail`, `skip`, or that delete assertions to make CI pass are a HIT
      unless the review explicitly justifies each one.
