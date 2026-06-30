---
description: Customize the per-role models for the redteam harness (plan / execute / review / rescue) and write them to .redteam/config.toml. Use to choose which model plans, which implements, and which independent model reviews.
---

Interactively configure the redteam harness's **per-role models** and persist
them to `.redteam/config.toml` `[models]`. This is the human-owned control
surface for the harness — the operator decides the model lineup, not an agent.

The four roles (config keys):

| Role | Key | What it does | Guidance |
|------|-----|--------------|----------|
| Plan | `planner` | Designs the outcome/plan | Top model — planning is high-stakes reasoning. Default/recommended: a top Opus (e.g. `claude-opus-4-8`). |
| Execute | `implementer` | Writes the code | Cheaper model is fine — implementation is token-heavy and routine (e.g. `claude-sonnet-4-6`). Selecting the worker *provider*: a value of `"codex"` runs the worker on Codex (role reversal); a `claude-*` model runs it on Claude. |
| Review | `reviewer` | Independent adversarial review | A **provider identifier**, not a model name: `"codex"`, `"claude"`, or `"human"` (manual). |
| Rescue | `rescue` | Escalation when review keeps failing | Same provider identifiers as `reviewer`. |

Steps:

1. Confirm `.redteam/config.toml` exists (harness vendored). If not, tell the
   user to run `/redteam:install` first. Read and show the current
   `[models]` values.
2. Ask the user what they want for each of the four roles (offer the current
   value as the default; offer the recommended defaults above). Use a clear
   question per role.
3. **Enforce the cross-model invariant before writing — this is the whole point
   of the harness.** The reviewer must resolve to a *different provider* than the
   worker, or the code gets reviewed by the same model that wrote it
   (self-review), which defeats redteam entirely:
   - Worker provider = `"codex"` if `implementer` is exactly `"codex"`, else
     `"claude"` (any `claude-*` model).
   - Reviewer provider = `"codex"` or `"claude"` if `reviewer` is one of those;
     `"human"` is a manual reviewer and is always allowed (a human is a distinct
     adversary).
   - If the chosen `reviewer` provider **equals** the worker provider (e.g.
     `implementer = "claude-sonnet-4-6"` with `reviewer = "claude"`), REFUSE to
     write it. Explain that this is self-review and ask the user to pick a
     different reviewer provider (e.g. `"codex"`), set `reviewer = "human"`, or
     change the worker. Do the same check for `rescue`.
4. Write only the `[models]` keys back to `.redteam/config.toml`, preserving the
   rest of the file and its comments. Do not touch any other section.
5. Show the final `[models]` block and remind the user that `reviewer`/`rescue`
   providers need their CLIs installed and authenticated (e.g. `codex login` for
   a `codex` reviewer).

Never silently "fix" a self-review config by changing a value the user didn't
choose — surface the conflict and let the human decide.
