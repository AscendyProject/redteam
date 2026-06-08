# Test conventions — Nuxt-like frontend

Facts a sub-agent needs to add a passing test. If a sub-agent changes the test
setup, refresh this file in the same change.

## Layout
- Unit/component tests: `tests/**/*.spec.ts` (Vitest). This is the `test_dir` +
  `test_file_glob` the harness recognizes (`tests/`, `*.spec.ts`).
- End-to-end tests: `e2e/**/*.spec.ts` (Playwright) — a **separate** runner, not
  part of the `npm test` unit gate. Don't put Playwright specs under `tests/`.
- Vitest config: `vitest.config.ts` (or the `test` block in `nuxt.config`). No
  per-folder config.

## Runner & environment
- `npm test` runs Vitest once (CI mode, no watch).
- Component tests use `environment: "happy-dom"` (or `jsdom`) — set in the Vitest
  config, don't re-declare per file.
- Nuxt-aware tests use `@nuxt/test-utils` (`mountSuspended`, `mockNuxtImport`) so
  auto-imports (`useRoute`, `useState`, composables) resolve. Plain component
  tests use `@vue/test-utils` `mount`.

## Writing a component test
- Mount with `@vue/test-utils` `mount(Component, { props, global })`.
- Stub child components you don't exercise via `global.stubs`.
- Query by `data-testid` (`wrapper.find('[data-testid="…"]')`), not by CSS class
  or text — testids are stable across copy/style changes.
- Assert behavior (rendered output, emitted events), not implementation details.

## Pinia
- `setActivePinia(createPinia())` in a `beforeEach`, or
  `createTestingPinia()` from `@pinia/testing` when you want stubbed actions.
- Don't reach into a store's private state — assert via its getters/actions.

## i18n in tests
- Install a real `vue-i18n` instance with the actual `i18n/locales/*` messages
  (via `global.plugins`) so `t('…')` returns real strings. A test that asserts a
  new key exists must check it resolves to a non-empty value in **every** locale
  (mirrors the atomic-i18n hard rule).

## Mocking
- Mock network with `mockNuxtImport('useFetch', …)` / `vi.mock('#app')` or by
  stubbing the composable that wraps the call — don't hit a real endpoint.
- `vi.mock` factories are hoisted; keep them at module top level.

## Gaps a sub-agent should NOT silently fill
- No global fetch fixture — stub per test at the composable boundary.
- Playwright/e2e setup is separate; don't add Playwright deps to satisfy a unit
  test. If a unit-level fixture is missing, add it and update this file in the
  same change rather than inlining ad-hoc setup.
