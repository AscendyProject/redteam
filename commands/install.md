---
description: Vendor the redteam agent-pair harness into the current project (runs the bundled installer). Use when setting up redteam in a repo for the first time.
---

Install the redteam harness into the user's project.

The `redteam-install` executable is on PATH while this plugin is enabled, and it
self-locates the bundled harness source. Run it against the project root
(default: the current working directory):

```bash
redteam-install .
```

- Add `--dry-run` to preview what would change without writing.
- Add `--overwrite` to refresh harness-owned files (`.redteam/workflows`,
  `prompts`, `templates`, `scripts/install.py`, and the seven `.claude/agents/*`
  skeletons, including `goal-decomposer`). Project-owned files — `.redteam/config.toml`, `.redteam/docs/*`,
  `.redteam/scripts/verify.sh`, and `.redteam/batches/` — are seeded once and
  never overwritten, so a re-run never clobbers the user's edits or task state.

After installing, point the user at `.redteam/config.toml` and `.redteam/docs/*`
to fill in for their stack (a complete example lives in
`examples/fastapi-like/`), then drive a task:

```bash
python3 .redteam/workflows/orchestrator.py start .redteam/batches/<batch>
```

External CLIs the harness shells out to — `python3`, plus the `claude` and/or
`codex` model CLIs named in `.redteam/config.toml` `[models]` — must be installed
and authenticated by the user; the plugin does not bundle them.
