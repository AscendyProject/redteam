# Outcome — <task title>

## Goal
<1–2 sentence statement of what success looks like, in user-facing or behavior-facing terms.>

## Done-when
- [ ] <Auto-verifiable condition 1>
- [ ] <Auto-verifiable condition 2>

## Out of scope
- <Explicit exclusion — things a reasonable reader might assume but you are NOT doing>

## Affected files
- `path/to/source-file` — <one-line reason>
- `(new) <test_dir>/<new test file matching your test_file_glob>` — <one-line reason — the canonical test location for your stack (per config.toml), NOT under `<task_dir>/`>

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Notes
- <Any targeted tests or manual checks that explain why these commands cover the change>

## Risks
- <Decision the human must make, or unknown that could expand scope>
