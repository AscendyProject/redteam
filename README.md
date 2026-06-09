# redteam

[![CI](https://github.com/AscendyProject/redteam/actions/workflows/ci.yml/badge.svg)](https://github.com/AscendyProject/redteam/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Runtime deps: 0](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)

An adversarial **agent-pair** harness for shipping code with AI. One model
drives a task through a test-first pipeline (plan → test → implement); a
**different** model reviews the work adversarially; humans gate the
irreversible steps. The collision of two independent model perspectives is the
point — automatic self-agreement is what it exists to prevent.

> Status: early. redteam was built as one project's internal harness and then
> extracted into this standalone repo, which owns it going forward — it has
> driven real, merged pull requests. (Its early git history reflects that origin,
> including cross-repo coordination from the parent project.) APIs and layout may
> still move.

**Quick install (Claude Code) — two commands:**

```text
/plugin marketplace add AscendyProject/redteam
/plugin install redteam@ascendy-redteam
```

Not on Claude Code? Vendor it into any repo — see [Install](#install).

## What it does

Given a batch of tasks (each a short `input.md` brief), the orchestrator walks
every task through a fixed pipeline, persisting `state.json` after each phase so
a run is fully resumable, retrying on `CHANGES_REQUESTED`, and **blocking at
human gates** (sentinel files you touch to approve):

```mermaid
flowchart TD
    PO[plan_outcome]:::worker --> PR[plan_review]:::rev
    PR --> HG1{{🔒 human gate}}:::gate
    HG1 --> IMPL[implement]:::worker
    IMPL --> RC[review_code]:::rev
    RC -->|APPROVED| CPR[create_pr]:::worker
    RC -->|CHANGES_REQUESTED| IMPL
    RC -. review fails twice .-> RES[rescue]:::rev
    RES --> HG2{{🔒 human gate}}:::gate
    HG2 --> CPR
    CPR --> HG3{{🔒 human gate}}:::gate
    HG3 --> DONE([done]):::done

    classDef worker fill:#e3f2fd,stroke:#1976d2,color:#0d47a1;
    classDef rev fill:#fce4ec,stroke:#c2185b,color:#880e4f;
    classDef gate fill:#fff8e1,stroke:#f9a825,color:#000;
    classDef done fill:#e8f5e9,stroke:#388e3c,color:#1b5e20;
```

<sub>Blue = **worker** model (writes) · pink = **reviewer** model (adversarial, fresh) · yellow = **human gate**.</sub>

That is the default **agent-pair** flow (a separate reviewer model reviews the
implementer). The alternative single-model **TDD** mode (`mode = "tdd"` in a
task's `state.json`) replaces `plan_review` with a `write_test → verify_test`
pair before `implement`; the gates and the `review_code → … → create_pr` tail
are the same.

Each phase is run by a focused sub-agent with its own prompt and tool scope
(`.claude/agents/*.md`): an outcome-planner, test-author/verifier, implementer,
code-security-reviewer, and pr-author. The reviewer is a *fresh* agent that only
sees the diff and the project's security checklist — it never sees the
implementer's reasoning.

## How it's different

A plain "two-model" setup stops at *a second model takes a second look*. redteam
makes that separation structural and then acts on it:

- **Findings are tiered, not pass/fail.** The reviewer emits findings with a
  severity (`blocker` / `major` / `minor`), and the orchestrator tracks each one
  *across review rounds* (a carry-over count) — a review is not a single thumbs
  up/down.
- **Persistent problems escalate on a ladder.** A blocker that survives multiple
  rounds climbs: retry the worker → a heavier `rescue` pass → hand to a human
  (`ask_user`). So one rejection doesn't kill a run, and a stubborn real bug
  doesn't get rubber-stamped after a single retry.
- **The reviewer is blind to the writer.** It's a fresh agent — and a
  configurably *different* model — that sees only the diff and the security
  checklist, never the implementer's reasoning, so self-justification can't
  cross the boundary.
- **Humans gate the irreversible steps** (plan approval, PR creation). It never
  autonomously merges.
- **Either model on either side, zero runtime deps.** See
  [Model freedom](#model-freedom) and [Install](#install).

## Model freedom

Roles bind to providers through a small adapter registry, not hardcoded calls.
Today Claude and Codex can each take **either** side:

| role | providers implemented |
|------|-----------------------|
| worker (planner / implementer) | `claude`, `codex` |
| reviewer / rescue | `codex`, `claude` |

You choose per role in `.redteam/config.toml [models]`. A reviewer value that
isn't an adapter (e.g. `"human"`) falls back to the manual flow (you paste the
review and touch the sentinel). Adding another provider is one adapter file plus
one registry line.

## When to use it

Not every change earns an adversarial agent pair. redteam is the **heavyweight
path** — a second independent model, human gates, a test-first loop — worth it
when a wrong call is expensive, overkill for a typo. The intended mental model is
to **scale the response to the risk of the change**:

| change | response |
|---|---|
| trivial / non-behavior-changing (rename, comment, formatting) | just do it — single agent, no harness |
| routine / small, local, reversible | single-agent loop; independent review optional |
| **guarded** — behavior change with real blast radius (auth, storage, concurrency, public API, migrations) | **this harness**: agent-pair + security review + gates |
| **strategic** — architectural or irreversible | the harness + heavier review (more reviewers, a human design gate) |

Two levers match effort to risk without forking the pipeline:

- **Model per role** (`config.toml [models]`) — a cheaper implementer for routine
  work, a frontier reviewer for guarded work; either provider on either side.
  Spend the expensive model where the risk is.
- **The escalation ladder** — findings carry a severity, and a blocker that
  survives rounds climbs retry → `rescue` → human. Effort concentrates where a
  problem actually persists.

Today you pick the tier yourself, by choosing which changes go into a batch.
**Automatic, tier-aware routing** — the harness classifying a task and scaling
its own phases and models — is
[on the roadmap](https://github.com/AscendyProject/redteam/issues/13), not in
this release.

## Install

### As a Claude Code plugin (recommended)

This repo doubles as a single-plugin marketplace, so two commands install it:

```text
/plugin marketplace add AscendyProject/redteam
/plugin install redteam@ascendy-redteam
```

That registers the six sub-agents and a `/redteam:redteam-install` command. Run
that command (or the `redteam-install` tool it puts on PATH) from your project
root to vendor the harness in:

```text
/redteam:redteam-install        # vendors .redteam/ into the current repo
```

### Or vendor directly (any stack, no Claude Code needed)

```bash
# from a clone of this repo:
python3 .redteam/scripts/install.py /path/to/your/project

# preview first:
python3 .redteam/scripts/install.py /path/to/your/project --dry-run
```

Either way it's the same vendoring model: the harness ships *inside* your project
tree (`.redteam/`) because the engine resolves your repo root from its own file
location. Harness-owned files (`workflows/`, `prompts/`, `templates/`, agent
skeletons) are re-vendored on each run (`--overwrite` to refresh); project-owned
files (`config.toml`, `docs/*`, `verify.sh`, your `batches/`) are seeded once and
never overwritten.

The installer does **not** vendor the harness's own unit tests, so a consumer
never runs (or maintains) them — your `verify.sh` runs *your* tests, not the
engine's. The vendored `.redteam/` engine follows the harness's own style, so
**exclude `.redteam/` from your project's linter/formatter** (e.g. ruff's
`extend-exclude`, an eslint ignore) to avoid it flagging code you don't own.

### Requirements

- Python 3.11+ (stdlib only — zero runtime pip dependencies).
- The model CLIs you configure, installed and authenticated:
  [`claude`](https://claude.com/claude-code) and/or `codex`.

## Configure

Edit `.redteam/config.toml` for your stack (paths, `verify_command`,
`branch_prefix`, role→model), then fill the three project docs the sub-agents
read:

- `.redteam/docs/project-context.md` — stack + hard rules
- `.redteam/docs/security-checklist.md` — the reviewer's hard lines
- `.redteam/docs/test-conventions.md` — how your test suite is wired

Two complete examples to copy the shape from: `examples/ascendy-like/` (Python —
FastAPI + Celery + Postgres + Milvus) and `examples/nuxt-like/` (JS/TS — Nuxt 3 +
Vue + Vitest).

## Run

```bash
python3 .redteam/workflows/orchestrator.py start  .redteam/batches/<batch>
python3 .redteam/workflows/orchestrator.py resume .redteam/batches/<batch>
python3 .redteam/workflows/orchestrator.py status .redteam/batches/<batch>
```

A batch is a directory of `tasks/<task-id>/input.md` briefs. The orchestrator
creates a per-task branch (`<branch_prefix>/<task-id>`), runs the pipeline, and
stops at each human gate until you touch the sentinel file it names.

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](.github/CONTRIBUTING.md) for the
dev setup and the gate (`bash .redteam/scripts/verify.sh`), and the
[Code of Conduct](.github/CODE_OF_CONDUCT.md). The engine stays
project-agnostic and stdlib-only — those two invariants drive most review
feedback. To report a vulnerability, see [SECURITY.md](.github/SECURITY.md)
(don't open a public issue).

## License

GNU AGPLv3 (`LICENSE`). Contributions are accepted under the
[Contributor License Agreement](CLA.md), which preserves the option of an
alternative/commercial license.
