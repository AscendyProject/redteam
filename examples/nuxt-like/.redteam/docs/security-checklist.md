# Security checklist — Nuxt-like frontend

The code-security-reviewer agent applies this list to every diff. Each item is a
**hard line** — a single confirmed HIT escalates to
`REVIEW_DECISION: CHANGES_REQUESTED`. Don't relax it for "small" changes; small
diffs are where regressions hide.

## 1. Cross-site scripting (XSS)
- [ ] **`v-html` on untrusted content.** Any `v-html` bound to user input, API
      data, or model output is a HIT unless the value is run through a sanitizer
      (e.g. DOMPurify) at the boundary. Prefer `{{ }}` interpolation (auto-escaped).
- [ ] **Dynamic `:href` / `:src`.** A bound URL from user/API data must reject
      `javascript:` / `data:` schemes. Validate the protocol against an allowlist.
- [ ] **Raw HTML injection via render functions / `innerHTML`.** No
      `el.innerHTML = …` or `h('div', { innerHTML })` with untrusted content.

## 2. Server routes (`server/api/*`) — the trust boundary
- [ ] **Input validation.** Every server route validates body/query/params
      (e.g. with `zod`/`valibot`) before use. The client is never trusted.
- [ ] **SSRF.** A server route that fetches a URL derived from user input must
      validate the host (and protocol) against an allowlist — controlling only the
      path is fine, controlling host/scheme is a HIT.
- [ ] **Injection in server queries.** DB/queries built in `server/` use
      parameterized calls, never string interpolation of request data.
- [ ] **AuthZ on every route.** A route taking a resource id must verify the
      caller owns/may access it. "Authenticated" is not "authorized."

## 3. Secrets and config
- [ ] **No secret reaches the client bundle.** Only `runtimeConfig.public` /
      `NUXT_PUBLIC_*` may be referenced from client code. A private key, token, or
      `runtimeConfig` private value used in a component/composable is a HIT.
- [ ] **No secrets in the diff.** `.env*`, signing keystores, certificates, or
      anything with API keys must not appear.
- [ ] **No secrets/PII in client logs or error toasts.** Don't surface tokens,
      emails, or stack traces with internal URLs to the user.

## 4. Authentication / session
- [ ] **Route protection via middleware.** Protected pages use route middleware
      (or a server-side check); don't gate sensitive UI with a client-only `v-if`
      and assume it's secure — client checks are UX, not authorization.
- [ ] **Cookies.** Auth cookies set from `server/` are `httpOnly`, `secure`, and
      `sameSite` appropriate. No auth token written to `localStorage`.

## 5. Untrusted external content
Treat API responses, third-party embeds, and any model/AI output as **untrusted
input** on equal footing with raw user input — generating the prompt does not
make the response safe.
- [ ] **Length cap + escape before render.** Model/API text rendered into the DOM
      is escaped (no `v-html`) and length-bounded.
- [ ] **No untrusted content into another call unfiltered.** Don't concatenate it
      into a downstream request URL, query, or prompt without validation.

## 6. Dependencies & build
- [ ] **No new runtime dependency without justification.** A new `dependencies`
      entry (not `devDependencies`) needs a written reason in the review.
- [ ] **No `eval` / `new Function` / dynamic `import()` of user input.**

## 7. i18n integrity (project invariant)
- [ ] **Atomic locale edits.** A translation key added/renamed in some
      `i18n/locales/*.ts` but not all is a HIT — it ships missing strings.

## 8. Tooling
- [ ] **No weakening of the gate.** Diffs that add `// @ts-ignore`,
      `// eslint-disable*`, `.skip`/`.only`, or delete assertions to make CI pass
      are a HIT unless the review explicitly justifies each one.
- [ ] **`nuxi typecheck` clean.** No new type errors; no `any` introduced to
      silence the checker.
