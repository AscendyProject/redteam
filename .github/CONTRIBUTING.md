# Contributing to redteam

Thanks for your interest. redteam is an adversarial **agent-pair** harness; it is
developed with the same discipline it embodies, so a few conventions matter.

## Ground rules (read before a non-trivial PR)

- **The engine stays project-agnostic.** No stack/project fingerprints (e.g. a
  hardcoded `pytest`, `app/`, a specific repo's rules) in `.redteam/workflows/`
  or in non-example tests. Project specifics belong in `.redteam/config.toml` +
  `.redteam/docs/*`, or under `examples/`. `test_agents_generic_prompts.py`
  guards this — keep it green.
- **Zero runtime dependencies.** The engine imports only the Python stdlib.
  Adding a pip dependency is a deliberate, reviewed decision — it breaks the
  "vendor and run" promise. Dev tools (ruff, pytest) are the only exception.
- **Don't loosen the security boundaries inline.** The verification allowlist,
  the installer's file-class split (harness-owned vs project-owned), the
  snapshot/fail-closed logic, and the adapter trust model are trust boundaries.
  Changes there get extra review — explain the boundary in your PR.
- **By contributing you agree to the [CLA](../CLA.md)** (it keeps provenance
  clean and preserves licensing flexibility). The project is licensed
  Apache-2.0.

## Dev setup

Python 3.11+ (the engine uses `tomllib`). Only two dev tools are needed:

```bash
python3 -m venv venv && source venv/bin/activate
pip install ruff pytest
```

## The gate

Every change must pass the project gate before you open a PR:

```bash
bash .redteam/scripts/verify.sh      # ruff check .redteam/ + pytest .redteam/tests
```

CI runs the same gate on every PR. Green is required.

## Workflow

1. Fork, branch off `main` (`<type>/<short-topic>`, e.g. `fix/empty-diff`).
2. Make the change. Match the surrounding style; keep diffs surgical.
3. **Add or update tests.** Bug fix → a test that fails before and passes after.
   Feature → tests that pin the new behavior. New tests live in
   `.redteam/tests/` (these are NOT vendored into consumers).
4. Run `bash .redteam/scripts/verify.sh` until green.
5. Open a PR. Use a clear title (conventional-commit style appreciated:
   `fix(...)`, `feat(...)`, `docs(...)`) and fill in the PR template.

Security-boundary or multi-file changes are reviewed harder (mirroring the
agent-pair discipline). Smaller, well-scoped PRs merge faster.

## Reporting

- Bugs / features: open an issue (templates provided).
- Security vulnerabilities: **do not** open a public issue — see
  [SECURITY.md](SECURITY.md).
- Conduct: see the [Code of Conduct](CODE_OF_CONDUCT.md).
