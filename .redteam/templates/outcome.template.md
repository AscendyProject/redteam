# Outcome — <task title>

## Goal
<1–2 sentence statement of what success looks like, in user-facing or behavior-facing terms.>

## Done-when
- [ ] <Auto-verifiable condition 1>
- [ ] <Auto-verifiable condition 2>

## Out of scope
- <Explicit exclusion — things a reasonable reader might assume but you are NOT doing>

## Affected files
- `path/to/file.py` — <one-line reason>
- `(new) tests/<area>/test_<feature>.py` — <one-line reason — canonical pytest location, NOT under `<task_dir>/tests/`>

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Notes
- <Any targeted tests or manual checks that explain why these commands cover the change>

## Risks
- <Decision the human must make, or unknown that could expand scope>
