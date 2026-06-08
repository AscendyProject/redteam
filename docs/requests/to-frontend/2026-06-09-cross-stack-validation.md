# [STAGED] Handoff to ascendy-frontend — redteam cross-stack validation (#7.5)

> **Status: staged, NOT yet dispatched.** This is the redteam session's first
> coordination act. When the redteam session runs, copy the body below into
> `ascendy-frontend/docs/requests/from-redteam/2026-06-09-cross-stack-validation.md`
> and cmux-notify the frontend surface (workspace:3 surface:16). Reply path is
> `redteam`'s own `docs/requests/from-frontend/` (not backend). Drafted from the
> backend session during redteam graduation; ownership now belongs to redteam.

---

# Request: validate the redteam harness on the frontend stack (#7.5 gate)

Date: 2026-06-09 KST
From: redteam (agent-pair harness project)
To: Frontend Claude main (+ Codex reviewer)
Type: cross-repo validation ask — NOT a product change. ~1 real task end-to-end.
Priority: low / no deadline. Schedule it around your real queue.

## What this is

The agent-pair harness has been extracted into a standalone, project-agnostic
repo: **`AscendyProject/redteam`** (private, AGPLv3). Before it goes public it
needs one proof it works **outside Python** — that no Python/ascendy coupling
leaked into the engine. Your repo (Vite/React/TS, eslint + vitest + playwright)
is the cleanest non-Python stack we have, so you're the ideal validator.

A **solo coupling smoke** already ran (your repo cloned to a throwaway dir,
installed, engine driven without model calls): install / config load /
orchestrator startup / branch setup / review+PR diff-base are all clean on your
stack. One coupling was found **and already fixed** (F-1: the verification
allowlist was Python-hardcoded `{pytest,ruff,mypy}` — now `verification_allowlist`
in config, so you use `[vitest,eslint,tsc]`). You should NOT hit it.

What the smoke can't give is **live usability**: does a different agent pair find
it usable end-to-end on its own stack. That's the ask.

## What we need you to do

1. **Install (simulate "download a release" — clone, don't symlink):**
   ```bash
   git clone git@github.com:AscendyProject/redteam.git /tmp/redteam-src
   python3 /tmp/redteam-src/.redteam/scripts/install.py .   # from your repo root
   # preview: ... install.py . --dry-run
   ```
   Vendors `.redteam/{workflows,prompts,templates}` + the 6 agent skeletons into
   `.claude/agents/`, and seeds `.redteam/{config.toml,docs/*,verify.sh,batches/}`
   (one-time; never overwrites files you've edited, even with `--overwrite`; it
   will not touch any `.claude/agents/*` you already own).

2. **Fill the project-owned files for your stack:**
   - `.redteam/config.toml`:
     ```toml
     [project]
     name = "ascendy-frontend"
     source_dirs = ["src/", "components/", "pages/"]
     test_dir = "tests/"            # or wherever your specs live
     test_file_glob = "*.spec.ts"
     verify_command = "npm run lint && npm run test"
     verification_allowlist = ["vitest", "eslint", "tsc"]
     branch_prefix = "fe"
     base_branch = "main"
     [models]
     planner = "claude-opus-4-7"
     implementer = "claude-sonnet-4-6"
     reviewer = "codex"
     rescue = "codex"
     ```
   - `.redteam/docs/{project-context,security-checklist,test-conventions}.md` —
     seeds are generic `<fill me>` templates; replace with your real stack/rules/
     test wiring. A complete real example: `/tmp/redteam-src/examples/ascendy-like/`
     (copy the shape, not the Python content).
   - `.redteam/scripts/verify.sh` — your gate (or rely on `verify_command`).

3. **Run ONE small real task end-to-end:**
   ```bash
   mkdir -p .redteam/batches/smoke/tasks/task-001-<slug>
   $EDITOR .redteam/batches/smoke/tasks/task-001-<slug>/input.md   # a short brief
   python3 .redteam/workflows/orchestrator.py start .redteam/batches/smoke
   ```
   Pipeline: `plan_outcome → plan_review → [gate] → implement → review_code →
   [gates] → create_pr → [gate] → done`. It stops at each **human gate** (touch
   the sentinel it names).
   - **No real PR required**: run up to `create_pr`'s gate and don't approve it —
     that exercises plan/test/implement/adversarial-review on your stack without
     leaving a test PR.
   - Pick a genuinely small task (a tiny component tweak with testable behavior).

4. **Report back** into `redteam`'s `docs/requests/from-frontend/` (or a cmux
   ping to the redteam surface): did anything assume Python / `app/` / `pytest` /
   a backend path? Did install + config + run work? Where did it fight you? Any
   phase prompt that read wrong for a JS task?

## Heads-up / known constraints

- **Private repo access:** `AscendyProject/redteam` is private; your `gh`/SSH
  auth under AscendyProject should reach it. Ping if the clone 403s.
- **Model CLIs:** the harness shells out to `claude` and `codex` (per `[models]`);
  both must be installed + authenticated in your workspace.
- **Real git commits on a task branch** (`fe/task-001-…`); it stashes/branches off
  `base_branch` first. Run from a clean-ish tree, or on a throwaway clone if you'd
  rather keep your repo pristine (a throwaway clone is a valid #7.5 run too).
- This is the **strongest generic-ness proof short of an external user** — honest
  "this fought me here" feedback is the point, not a rubber stamp.

## Reply path

`redteam`'s `docs/requests/from-frontend/` or a cmux ping to the redteam surface.
No urgency — fit it around the 1.0.x work.
