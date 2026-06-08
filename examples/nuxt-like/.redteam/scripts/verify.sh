#!/usr/bin/env bash
set -euo pipefail

# JS/TS gate for a Nuxt 3 project. Assumes dependencies are already installed
# (npm ci / pnpm install) in the workspace — the harness does not install them.

echo "=== eslint ==="
npm run lint

echo "=== typecheck (nuxi) ==="
npx nuxi typecheck

echo "=== vitest ==="
npm test

echo "✅ verify.sh OK"
