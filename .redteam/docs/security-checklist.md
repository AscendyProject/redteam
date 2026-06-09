# Security checklist — <Project name>

> **Template.** The code-security-reviewer sub-agent applies this list to every
> diff (path from `config.toml [project] security_checklist`). Each item is a
> **hard line**: a single confirmed HIT escalates to
> `REVIEW_DECISION: CHANGES_REQUESTED`. Replace the items below with the rules
> that actually matter for your stack — keep them concrete enough that a
> reviewer can mark HIT/clean without guessing. For a fleshed-out, real example
> see `examples/fastapi-like/.redteam/docs/security-checklist.md`.

Don't relax the checklist for "small" changes; small diffs are where regressions hide.

## 1. Injection
- [ ] **SQL / query injection.** No query strings built by string interpolation
      from user-controlled (or user-influenced DB) values. Use bound parameters.
- [ ] **Search / expression injection.** Any DSL/expression language fed to your
      datastore must be built from validated structured input, never raw text.
- [ ] **Shell / subprocess injection.** No `shell=True` with user input; pass arg lists.

## 2. Path / file handling
- [ ] **Path traversal.** Normalize and bound user-supplied names/keys inside an
      allowed prefix; reject `..`, absolute paths, NUL bytes.
- [ ] **Object-store keys.** Don't concatenate user-controlled strings into storage keys.

## 3. Secrets and logs
- [ ] **No secrets in logs or error responses** (connection strings, tokens, keys, PII).
- [ ] **No secrets in commits** (`.env*`, key/cert files).
- [ ] **No PII in queue/task arguments** — pass IDs, hydrate in the worker.

## 4. AuthN / AuthZ
- [ ] **Every protected route goes through the auth dependency** — no custom bypass.
- [ ] **Per-user authorization on every read/write.** A route taking an `id` must
      verify the resource belongs to the caller. "Authenticated" ≠ "authorized."

## 5. External surfaces
- [ ] **Signed-URL TTLs are short**; no long-lived URLs without a written reason.
- [ ] **Inbound webhooks verify the provider signature** — don't trust the body.
- [ ] **Open redirect.** Validate any user-supplied redirect target against an allowlist.

## 6. Concurrency / idempotency
- [ ] **Background tasks are idempotent** with explicit retry policy and a
      deterministic effect when re-run with the same arguments.
- [ ] **Shared writes use atomic ops / row locks**, not read-modify-write in code.

## 7. Architecture invariants
- [ ] **No bypass of the project's required abstraction layers** (e.g. business
      logic importing a low-level client directly).
- [ ] **No infra calls from the wrong layer** (e.g. route handlers opening clients).

## 8. AI / model output trust boundary (if applicable)
Treat every model response as **untrusted input**, on equal footing with raw user input.
- [ ] **Length cap before persistence** of any model output.
- [ ] **Escape on render paths** (HTML/Markdown) for any model output reaching a response.
- [ ] **Prompt-injection sanitization** of text echoed back from hostile inputs.
- [ ] **No model output reaches a query/expr/shell/path** via string interpolation.

## 9. Tooling
- [ ] **Static security scanner clean** (e.g. `bandit -r <changed_files>`); HIGH severity is a HIT.
- [ ] **No weakening of tests / types / lint** to pass CI (`# type: ignore`, `# noqa`,
      `xfail`, `skip`, deleted assertions) without an explicit justification per line.
