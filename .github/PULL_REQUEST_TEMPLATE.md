<!-- Thanks for contributing! Keep PRs small and scoped. -->

## What & why

<!-- What does this change and why? Link any issue (e.g. Closes #12). -->

## Checklist

- [ ] `bash .redteam/scripts/verify.sh` is green (ruff + pytest)
- [ ] Tests added/updated (bug fix → a test that failed before; feature → tests pinning the behavior)
- [ ] Engine stays project-agnostic (no stack/project fingerprints in `.redteam/workflows/` or non-example tests)
- [ ] No new runtime dependency (engine is stdlib-only), or it's called out and justified below
- [ ] If this touches a security boundary (verification allowlist, installer file-class split, snapshot/fail-closed, adapter trust model), I've described the boundary and why the change is safe
- [ ] I agree my contribution is under the project [CLA](../CLA.md)

## Notes for reviewers

<!-- Anything that needs extra eyes: trade-offs, follow-ups, things you're unsure about. -->
