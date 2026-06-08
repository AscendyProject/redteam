# Nuxt-like frontend — Sub-agent context

Compact reference loaded by every sub-agent. Authoritative source is `CLAUDE.md`
at repo root — this file mirrors only what a sub-agent needs to make safe edits.

## Domain
A user-facing web/mobile app (SSR + SPA hydration). This example is generic; a
real project replaces these specifics with its own.

## Stack
- Nuxt 3 + Vue 3 (`<script setup lang="ts">`) + TypeScript (strict)
- Pinia stores; `composables/` for shared reactive logic
- `vue-i18n` with one file per locale under `i18n/locales/*.ts`
- Vitest + `@vue/test-utils` (+ `@nuxt/test-utils`) for unit/component tests
- Playwright for end-to-end (`e2e/`, separate from unit `tests/`)
- ESLint (flat config) + `nuxi typecheck` (vue-tsc) as the type gate
- Capacitor wraps the SPA build for iOS/Android (optional)

## Architecture entry points
- `app.vue` / `layouts/` — root shell and layouts
- `pages/` — file-based routes (a file here is a URL)
- `components/` — presentational + container Vue components
- `composables/use*.ts` — shared logic (data fetching, state, formatting)
- `stores/*.ts` — Pinia stores (typed state + actions)
- `server/api/*.ts` — Nuxt server routes (the only place server secrets are read)
- `i18n/locales/*.ts` — translation messages, one file per locale

## Hard rules (must respect when writing code)
- **i18n is atomic across ALL locales.** Adding/renaming a translation key means
  editing **every** `i18n/locales/*.ts` file in the same change. A key present in
  some locales but not others is a bug — never ship a partial set.
- **No user copy hardcoded in templates.** All visible strings go through `t('…')`.
- **Shared logic lives in a composable or store, not duplicated in components.**
- **Server secrets stay server-side.** Read private config only in `server/`; only
  `runtimeConfig.public` values may reach the client bundle.
- **No direct DOM mutation.** Use refs/reactivity; reach for `document`/`window`
  only behind `import.meta.client` guards, never during SSR.
- **Typed stores and props.** No `any` on store state, props, or composable returns.

## Architecture boundaries
- Components call composables/stores; they don't fetch from `server/api` directly
  with ad-hoc `fetch` — use `useFetch`/`$fetch` through a composable.
- Server routes are the trust boundary: validate every input there.
- Shared types live in `types/` (or co-located `*.d.ts`), imported, not redefined.

## Forbidden actions (sub-agents must refuse)
- `git push --force` to `main` or shared branches
- `rm -rf` outside `/tmp` or build artifacts (`.nuxt/`, `.output/`, `dist/`)
- Editing `.env*`, signing keystores, or anything containing secrets
- Touching generated output (`.nuxt/`, `.output/`) by hand
- Weakening tests, ESLint, or `nuxi typecheck` to make CI pass
  (`// @ts-ignore`, `eslint-disable`, `.skip`, deleting assertions)

## Verification
Sub-agents that write code run `bash .redteam/scripts/verify.sh`
(ESLint + `nuxi typecheck` + Vitest) and report failures back to the
orchestrator rather than papering over them.

## See also (do not load by default — too verbose for sub-agent context)
- `CLAUDE.md` — full collaboration rules
- `e2e/README.md` — Playwright scenarios + data-testid conventions
