# Model-combination benchmarking (#146) — design accepted, MVP-first

Status: **design ACCEPTED, v1 scoped** (operator decision 2026-07-13; crux
questions resolved with the maintainer). Implementation phases go through their
own `plan_review` before code — this note records the scope and the constraints
so the implementer carries them in.
Date: 2026-07-13. Context: #146 (proposal), #92 (reviewer-cost work that reuses
the same phase telemetry), #95 (the deterministic per-role model picker this
would inform). Supersedes nothing.

## Problem

Model choice for redteam's four roles (`planner` / `implementer` / `reviewer` /
`rescue`) is operator intuition. redteam is not one LLM call — it is a
role-separated loop where a cheaper implementer may be fine if a strong reviewer
catches regressions, a stronger planner may cut retries, etc. As providers ship
new frontier and mid-tier models, a good combination drifts. #146 asks for an
objective, **repo-specific** way to measure whole-loop quality / cost / latency /
safety per model combination and recommend presets.

## What shapes the design (constraints, verified against current code)

1. **Cost explosion is the dominant constraint.** A benchmark run is a
   combinatorial set of **real full-pipeline executions**. A 2⁴ matrix × 3 tasks
   × 3 repetitions is 144 pipeline runs ≈ $300–600 and hours of wall-clock. This
   is a heavyweight, occasional operation — **never a CI default**. Hard budget
   caps and dry-run sizing are mandatory, not optional.
2. **Telemetry is asymmetric.** The Claude adapter runs `claude --output-format
   json` and already receives `total_cost_usd` + `duration_ms`
   (`phase_runners/_base.py:175-176`) — today only *logged* (`DONE (786.1s,
   $2.431)`), not persisted. The **Codex adapter is stdout-only** (review body);
   it exposes **no cost/token**. So cost-per-role is measurable for Claude-driven
   roles and **n/a for Codex-driven roles** (reviewer/rescue). v1 reports what
   the CLI actually gives and marks Codex-role cost `null` — **no fabricated
   estimates**.
3. **Oracle quality is everything.** Only **deterministic oracles** are
   trustworthy: test pass/fail, review decision, review-round count, retry/rescue
   counts, finding severities (from review text), scope-creep (floor trips). An
   LLM-judge "quality_score: 0.86" is biased and must not be the headline.
4. **Zero runtime dependencies.** `sqlite3` / `json` / `statistics` are stdlib
   (available); pandas/numpy and promptfoo/LangSmith/Braintrust are **not** — the
   latter also violate the issue's own "no required commercial eval platform"
   non-goal. v1 is fully standalone.
5. **redteam's identity is preserved.** No auto-merge, no bypass of the human
   checkpoint; recommendations are scoped to the benchmark set + project, never
   claimed as universal rankings.

## Resolved crux decisions

- **Scope: Phase 0 + Phase 1 MVP first** (not the full matrix/Pareto/recommend/
  judge spec in one shot).
- **Quality metrics: deterministic-only** in v1 (LLM-judge deferred to Phase 2,
  clearly caveated when it lands).
- **Storage: JSONL-first** (append-resumable, zero-dep, greppable; sqlite mirror
  is a Phase 2 option, not v1).

## Phase 0 — telemetry persistence (foundation, always-on)

Persist per-phase telemetry to `state.json` that today is only logged:
`model` / `provider`, `cost_usd` (Claude; `null` for Codex), `duration_sec`,
review-round count, retry + rescue counts, terminal outcome. This is cheap, adds
no user-facing surface, and is **independently useful** — the #92 reviewer-cost
work and the `config` picker (#95) both want it. Phase 0 ships and stands alone
even if Phase 1 never does.

## Phase 1 — benchmark MVP

- **Dataset**: `.redteam/benchmarks/<set>/benchmark.toml` + `tasks/<id>/input.md`,
  each task carrying oracle fields (verification command, expected affected area,
  optional known-risk labels). **Curated tasks first**; converting historical
  `.redteam/batches/*` is a later convenience, not the initial surface.
- **`orchestrator benchmark <set>`**: for each **named `[models]` config** in the
  set (an explicit small list — **not** a full matrix expansion in v1), run the
  curated task-set in an **isolated git worktree** per candidate, capture Phase 0
  telemetry, and **append** results to JSONL. Resumable: a completed
  `(config, task, repetition)` is skipped on re-run.
- **Deterministic metrics** aggregated per config: final approval rate, test pass
  rate, review-round avg, blocker/major finding count, retry + rescue rate,
  scope-creep (floor-trip) count, wall-clock, and **Claude cost per approved
  task** (Codex-role cost shown `n/a`).
- **`orchestrator benchmark-report <set>`**: a markdown table **diffing configs**
  on those metrics. Tradeoffs are shown side-by-side — **no single hidden score,
  no Pareto frontier in v1**.
- **Cost controls (mandatory)**: `--dry-run` prints the run count + a rough cost
  estimate and exits without running; a `budget` cap in `benchmark.toml` aborts
  before it is exceeded; `repetitions` (default **1**) controls width.

## Deferred to Phase 2+ (only after the MVP loop is proven)

Matrix expansion (`[[benchmark.matrix]]`), Pareto frontier, `recommend-models
--profile performance|cost|balanced`, LLM-judge scorers (plan fidelity /
scope-creep, with provider separation and caveats), a sqlite mirror for querying,
and any optional external-platform export. None of these are v1.

## Issue open questions — resolved

1. **JSONL only** for v1 (sqlite a Phase-2 option).
2. **Cost telemetry**: Claude from `--output-format json`; **Codex n/a** in v1
   (adapter is stdout-only — no estimate fabricated).
3. **Curated tasks first**; historical conversion later.
4. **Standalone**; no promptfoo/LangSmith/Braintrust in v1.

## Non-goals (v1)

- No auto-merge / no bypass of the human merge checkpoint.
- No universal cross-repo model rankings — results are scoped to the benchmark
  set and project profile.
- No LLM-judge-only scoring.
- No required commercial eval platform / hosted SaaS.
