# Changelog

All notable changes to the redteam harness are recorded here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
releases may include behavior changes; breaking changes are called out).

## [Unreleased]

## [0.9.1] - 2026-08-28

A small release cut for one reason: **0.9.0's headline feature could not run.**
Preparing the first real Phase 1 benchmark sweep — the sweep whose absence 0.9.0
already flagged — surfaced a defect that made the runner unusable on any repo
with a virtualenv-based gate, and the review of the fix then surfaced a second,
worse one underneath it. Both are fixed here. Additive except the benchmark
record schema (v2 → v3), which still has no deployed data to migrate.

### Fixed
- **The benchmark tempcopy excluded `venv`/`.venv`, so a venv-based gate failed
  with exit 127 (#185, #186).** `run_one` hardcoded them into its `copytree`
  exclusions, so a repo whose `verify_command` activates a project-local
  virtualenv found no tools on `PATH` in the snapshot. Every verification exec
  failed, the task churned through its retries to `deferred`, and the run cost
  full price while recording metrics that described a broken environment rather
  than a model combination — worse than no data, because the records looked
  measured. Not repo-specific: any Python consumer with a venv-based gate hit it
  identically (`node_modules` is not excluded, so JS/TS consumers were
  unaffected). The exclusion list is now split — `.git`, `batches`, `results`
  and `results.jsonl` are **mandatory** and unioned in after the set's own list,
  because dropping `.git` would hand the pre-implement floors a trust root from
  the operator's history and dropping the others would expose real batch state;
  `venv`, `.venv`, `__pycache__` and `*.egg-info` are the **default** half a set
  may replace via a new top-level `copy_exclude` key in `benchmark.toml`
  (replacement semantics, not merge). Default behaviour is unchanged, so the
  loosening is opt-in and recorded in the set's own toml. It also moves four
  Python-stack fingerprints out of the engine into project-owned config.
- **A contaminated benchmark run was indistinguishable from a clean one (#186,
  review IR-001).** Opting a host path into the snapshot exposed a latent
  failure mode: the copied virtualenv is *not* relocatable — `bin/activate` and
  console-script shebangs hold the original absolute prefix — so verification
  executes the host toolchain, which every run of a sweep shares. A task that
  installs, upgrades or removes a package therefore changes the environment
  later configs are measured in, and the resulting ranking encodes dispatch
  order. The engine cannot prevent this generically (provisioning a per-run
  toolchain needs a stack-specific install command that would put project
  specifics back into the engine), so it detects it: opted-in host paths are
  fingerprinted around the run, records carry `host_mutated` (**schema v2 →
  v3**), `run_benchmark` aborts the sweep with **exit code 4** rather than
  paying to measure a changed environment, and `build_report` excludes marked
  records from every metric while showing the exclusion in the sample cell. As
  with v2's `rescue_count`, a pre-v3 record has no such key and "not checked"
  does not read as "clean" — only an explicit `true` excludes. The fingerprint
  is size + `mtime_ns`: it catches an honest `pip install`, and is deliberately
  not a tamper-proof control.
- **A `plan_review` backtrack rewrote `outcome.md` instead of amending it
  (#183, #184).** The planner regenerated the plan from scratch on every
  backtrack, destroying any edit a human made between rounds — the documented
  "fix, then `orchestrator resume`" escape — and reopening findings `state.json`
  already tracked as resolved, so the loop could not converge once a human had
  co-authored the document. Observed as six plan-review rounds ending in
  `RESCUE_REQUIRED` because the loop, not the plan, was the defect. The
  orchestrator now snapshots `outcome.md` to `outcome.round<n>.md` (exclusive
  create, so a planted symlink cannot redirect the write outside the task dir)
  and flags the next `plan_outcome` to amend in place. Applied at **all three**
  routes back to the planner, not just the `CHANGES_REQUESTED` backtrack — the
  `USER_DECISION: REVISE_PLAN` path is the one most likely to carry human edits,
  since the operator was explicitly asked to intervene.

### Notes
- **The benchmark's isolation is bounded, and now says so.** The snapshot
  isolates the *repo*, not the *toolchain*. `run_one`'s docstring claimed
  isolation without qualification; it is now qualified, and the boundary is
  pinned by a test rather than prose, so a future change that makes the copy
  relocatable trips the test and forces the claim to be updated with it.
- #146 Phase 1 **still has not run for real.** This release is what makes the
  attempt possible; Phase 2 continues to wait on evidence from actual use.

## [0.9.0] - 2026-08-16

This release ships the **#146 Phase 1 benchmark MVP** and is otherwise dominated
by a **downstream bug-report wave** that turned into a sustained pass over
review-artifact honesty. A vendored consumer on 0.4.0 filed seven issues; three
were already fixed upstream and closed as such, and the remaining four exposed
real gaps in what the harness records about its own reviews — which model
produced a verdict, which commit it judged, whether a metric was measured at all.
The third autonomous `/redteam:goal` run (batch `test-quality-gate`) shipped the
test-quality half. Everything is additive except the benchmark record schema
(v1 → v2), which has no deployed data to migrate.

### Added
- **#146 Phase 1 benchmark MVP (#148, #150, #154, #163).** `orchestrator
  benchmark <set-root> [--dry-run]` and `orchestrator benchmark-report
  <set-root>` run named model-combination configs from
  `.redteam/benchmarks/<set>/benchmark.toml` over a task set, append
  deterministic records to `results.jsonl`, and print a side-by-side markdown
  diff. Deterministic metrics only — sample size, approval rate, review rounds,
  retry/rescue/scope-creep, wall-clock, Claude cost per approved task. **No
  Pareto frontier, no single "score", no recommended winner**: the report is a
  tradeoff table an operator reads, per the accepted MVP-first design
  (`docs/decisions/2026-07-13-benchmark-design.md`). Runs are isolated
  (subprocess + repo tempcopy, `create_pr` neutralised, no `origin` remote),
  resumable, and budget-fenced. Phase 0 (#150) persists per-phase
  model/cost/duration/outcome to `state["phase_telemetry"]`. Cost honesty is
  structural: the Codex transport is stdout-only, so Codex-role phases record
  `null`, never an estimate. Phase 2 (Pareto, `recommend-models --profile`,
  LLM-judge scorers) remains deferred — #146 stays open for it.
- **Standalone reviews declare their mode and pin their range (#166, #177).**
  `orchestrator review` prepends a harness-emitted provenance header naming the
  mode, the reviewed commit, the base, and the reviewer. A standalone verdict
  asserts strictly less than an in-pipeline one — #103 suspends the
  verification and `outcome.md` Required Checks because no task directory
  exists — yet both rendered identically while the exit code can gate CI. The
  reviewer is now handed an immutable `<base_sha>...<head_sha>` range rather
  than `main...HEAD`, so the range recorded and the range read are the same
  object; movable refs left a window in which a branch could advance between
  sampling and the reviewer invoking git. Endpoints are pinned independently,
  and a HEAD that moves mid-review is reported rather than silently
  re-attributed.
- **Each standalone review is archived (#162, #178).** Reviews are written to
  `.redteam/reviews/<utc-ts>-<short-sha>.md`, with `last_review.md` kept as a
  copy. Previously every run overwrote a single file, so an operator saw one
  verdict with no way to know it was one of several — which is what made the
  reported run-to-run variance invisible. Repeated reviews of the *same* commit
  now sit side by side; the archive name is reserved by exclusive create, and a
  failed write removes its partial file rather than leaving a truncated record
  at the authoritative name. This is #162's **observability half only**; the
  gate change it also proposes (reproduce-before-block, or majority-of-N) is
  deliberately not attempted — it alters review-gate semantics and multiplies
  reviewer cost, and should be decided on recorded evidence rather than a single
  anecdote. #162 stays open for that.

### Changed
- **The "would have failed before" Required Check is tightened in both
  directions (#159, #161, #174).** The rule was simultaneously too weak and too
  strong: a *source-text guard* (read the file, assert a substring) satisfied it
  trivially while never executing the module — a consumer shipped a component
  that never mounted with three such tests green — and a *preventive suite*
  (smoke/characterization, green by construction) could never satisfy it,
  leaving deletion or gate-override as the only exits. It now has three named
  clauses: **A** flags source-text assertions that bypass an available execution
  path (the vacuity is the bypass, not the assertion); **B** gives preventive
  suites a *stricter* criterion — an executable demonstration via a deliberately
  broken fixture, in the same file, through the same code path, breaking the
  behaviour the suite claims to protect; **C** is a narrow per-artifact
  exemption for artifacts with no in-repo execution path, established by naming
  that artifact's consumers, never by file class or glob. No project-owned file
  may override any clause — a consumer's attempt to encode the exception in
  `project-context.md` was correctly refused by the reviewer, which is why this
  had to be fixed in the harness.
- **Plan fidelity is verified per item (#133, #180).** The reviewer was already
  given `outcome.md` and already told to check the implementation against it,
  but the one-line instruction produced no Done-when findings across ~19
  observed review rounds. It now demands the shape reviewers demonstrably
  comply with elsewhere in the same prompt: locate the Done-when list,
  adjudicate **each item** on its own line as met or unmet, flag an unmet item
  `severity:major`, and when no list exists say so and judge against the Goal
  rather than skipping silently. Task-scoped — the standalone suspension is
  untouched and regression-tested.
- **Reviewer phases now emit telemetry (#172, #176).** `review_code` and
  `plan_review` invoke a model but wrote no `phase_telemetry` entry, so the
  benchmark's `review_rounds` was structurally always 0 — a task re-dispatched
  twice on reviewer feedback still reported zero rounds. The materialization
  sentinel is broadened from "a worker was invoked" to "a model was invoked".
  Attribution matters more than the count: a new structured `provider_used`
  records the provider that **actually produced** the result, since a staged
  first-pass, an automatic fallback, and an exhausted fallback each run someone
  other than the configured reviewer. Values a reviewer transport cannot report
  stay `null`. `rescue` is unchanged and still emits nothing — it invokes no
  model, it validates a manually produced report.
- **Benchmark record schema is v2 (#172, #179).** `rescue_count` is nullable and
  sourced from a new durable `rescue_total_count`, incremented only past the
  rescue ceiling check so a refused terminal attempt is not counted;
  `rescue_entry_count` keeps its exact budget semantics (it bounds the #87
  runaway) and is untouched. A v1 record's `rescue_count: 0` was a *fabricated*
  zero — the old extractor counted `rescue` telemetry the engine never writes —
  so v1 values are excluded from both numerator and denominator, and the rate
  divides by measured records only. No deployed data needs migrating: the
  benchmark had not yet been run for real when this shipped.
- **`test_conventions_file` reaches agent-pair mode (#160, #171).** The project's
  test conventions were injected only into `write_test.py` / `verify_test.py`,
  both TDD-only phases that agent-pair skips — so in the default mode, where the
  implementer writes the tests, the document was dead config and the reviewer
  had nothing to judge tests against. Now injected into `implement.py` and
  `review_code.py`; TDD injection is unchanged and regression-tested.
- **CI pins the lint/test toolchain (#164).** `pip install ruff pytest` resolved
  to whatever had shipped to PyPI at job time, so ruff 0.16 turned `main` red
  with 158 findings in untouched files while pinned local venvs stayed green —
  the gate's verdict was decided outside version control. `ruff` and `pytest`
  are pinned in `ci.yml` and pyproject's `dev` extra; bump them deliberately.

### Fixed
- **A sibling task's `pr_url.txt` no longer trips the pre-worker floors (#158,
  #173).** `create_pr` writes it and never commits it, so in a stacked goal run
  every task after the first saw its parent's copy as an untracked outside-scope
  path and `_cross_run_trust_root_floor` failed closed **before the worker ran** —
  on a file the harness itself had just produced. Observed twice, including a
  task deferred after `plan_review` had already approved it. The basename joins
  `_SIBLING_BASENAME_ALLOWLIST` next to `pr.md`/`state.json`; every structural
  guard is unchanged (exact basename, top level of a sibling dir, same batch's
  `tasks/` root), and both floors share one predicate so they cannot drift.
- **`phase_telemetry` records the model (#168, #175).** `model` was `null` on
  every entry ever written, for both providers, while `cost_usd` and
  `duration_sec` from the same object were populated. The value lives on the
  Claude CLI's `system`/`init` event — the one that prints `init (model=…)` — but
  `run_claude` retained only the final `result` event, which carries no model.
  It is now captured during the stream and surfaced as `init_model`. The unit
  test could not have caught this: its fixture fabricated the key on a `result`
  dict, so the adapter was asserted to forward a field production never emits.
- **Codex review no longer falls back to manual under the read-only sandbox
  (#144, #147).** The review prompts instructed writing the review file and a
  `.done` sentinel, which the read-only adapter forbids, so Codex burned turns
  on blocked writes until the adapter timed out into `reviewer_fallback`.
  `## Output` is now channel-aware: the headless path outputs to stdout only,
  the manual path keeps the file instructions. Raw `codex exec` stderr remains
  deliberately unexposed (IR-002 — it can carry credentials).
- **`_plan_affected_files` parses the standard Affected-files bullet (#149,
  #151).** The parser stripped a backtick *run*, so `` - `path` — reason ``
  yielded `` path` — reason `` and a legitimately plan-declared path was not
  exempted, tripping the floor. It now extracts the first backtick span and
  discards trailing prose; bare paths are cut at the first separator.
- **A verification command missing from PATH fails closed legibly (#152,
  #153).** A command that passed the allowlist but was not resolvable raised a
  raw `FileNotFoundError` that the batch driver reported as an opaque
  `error: FileNotFoundError`. It now returns a non-zero code naming the command
  and pointing at the project verify wrapper.

### Notes
- **Stacked-merge hazard, observed.** #155/#156 were merged into their parent
  branches rather than `main` — all three Phase 1 PRs merged inside 24 seconds,
  so GitHub never retargeted the stacked bases — and the runner, report and CLI
  sat stranded off `main` until #163 recovered them. After squash-merging a
  parent, **verify the child PR's base actually retargeted to `main` before
  merging it**; the trap nearly recurred on #174 in the same session.
- Consumer issues #144, #149 and #165 were filed against a vendored 0.4.0 tree
  and were already fixed upstream. A vendored `.redteam/` is a copy: it updates
  only when `install.py` is re-run. `install.py --check` reports the vendored
  version.

## [0.8.0] - 2026-07-07

This release is dominated by **goal mode becoming autonomous and then proving
itself on the harness's own backlog.** `/redteam:goal` is reworked from a
one-shot into a run-to-completion driver, backed by a new machine-readable
`status --json`; two real autonomous goal runs then drove the rest of this
release end-to-end — the first shipping #92's reviewer-cost pair (P3 round-staged
reviewer, P5 hard ceilings) and the second closing the two floor gaps (#136/#137)
the first run had surfaced. A planner-skeleton fix (#138) removes a recurring
`plan_review` false-block those runs kept hitting. Everything is additive and
opt-in; there are no breaking changes.

### Fixed
- **Goal-mode floors are now harness-artifact-aware (#136, #141).** The
  pre-worker out-of-scope floor (#91) and the cross-run trust-root floor (#117)
  no longer fail-closed on the harness's own decision trail: same-batch
  top-level decompose artifacts (`goal.md`, `goal.json`, `decompose_review.md`,
  `decompose_blocked.md`) and a sibling task's top-level `input.md` are exempt,
  via a single shared `_is_harness_artifact` predicate so the two floors cannot
  drift apart (the #117 Check-2 ↔ #124 sibling-allowlist inconsistency that
  self-locked a stacked run is closed). Genuine operator WIP outside scope is
  still refused; the adversarial-baseline-rewrite guard is preserved.
- **Pre-worker floor honors plan-declared Affected files (#137, #142).** Paths
  explicitly listed in the current task's review-approved `outcome.md` Affected
  files (tolerating the `(new) ` prefix) are exempt, so a review backtrack no
  longer self-locks on the worker's own round-1 output. The exemption is
  snapshotted **set-once** before the first worker run and never re-read from
  the live file — a failed round cannot widen its own exemption. A post-commit
  integrity layer (`_uncommitted_plan_affected_paths`) closes the reviewed-range
  gap a cross-model stack review found: an exempted outside-scope tracked path
  can no longer escape the committed `base...HEAD` range while verification runs
  green against the worktree. `_cross_run_trust_root_floor` is deliberately NOT
  on the exemption path. Both #136 and #137 shipped by the **second autonomous
  `/redteam:goal` run** (batch `floor-hardening`).
- **outcome-planner emits a parseable `## Verification` block (#138, #143).** The
  planner skeleton instructed a prose `## Verification hooks` section, but the
  `plan_review` gate parses a section titled exactly `## Verification` with a
  fenced `yaml` `commands:` list — so the planner's own output kept failing the
  gate, burning 1–3 frontier review rounds per task and escalating
  otherwise-approved plans to `ask_user`. The skeleton now emits the parseable
  block (kept stack-neutral — placeholder verify command); the TDD agents'
  `Verification > Existing/To be created` pointers are realigned, and a
  regression runs the real gate parser over the skeleton body. Engine unchanged.

### Added
- **#92 P3 — opt-in round-staged reviewer model (#135).** The `review_code`
  round loop can run its first-pass scan on a cheaper reviewer model and
  promote to the configured frontier reviewer as findings persist across
  rounds. Approval authority never downgrades: a cheap first-pass may reject
  early (CHANGES_REQUESTED) but can never APPROVE — `PhaseResult(approved)`
  only ever comes from the frontier reviewer, and a first-pass APPROVED
  triggers same-round frontier promotion. Off unless configured; the unstaged
  path is byte-identical to before. Shipped by the **first autonomous
  `/redteam:goal` run** (batch `reviewer-cost-p3p5`).
- **#92 P5 — opt-in hard ceilings on the review loop (#140, review trail in
  #139).** `[models.review_ceilings]` adds `max_review_rounds` and
  `max_wall_clock_sec` on top of the retry/rescue ladder; crossings return a
  structured ceiling-terminal result (`ceiling_hit`) that defers — never
  approves, never rescues. The wall-clock ceiling is the outermost exit,
  including on the MANUAL_REQUIRED fallback path (round-4 review fix). With no
  ceilings configured there are no counters, clock reads, or state growth.
  Prompt caching was evaluated and documented as a no-op at the current CLI
  adapter seams (`docs/decisions/2026-07-05-reviewer-prompt-caching.md`).
- **`orchestrator status <batch> --json` — machine-readable, goal-aware status.**
  Emits per-task `next_phase` / completed phases / gate sentinels / deferral
  records (safe fields only — never `last_failure_log` or a deferral's
  `feedback`, both of which can carry secrets quoted from raw stderr or a diff)
  plus, when the batch has a `goal.json`, goal progress (`total`/`done`/
  `complete`/`incomplete_ids`/`deps`). On an invalid manifest, status *reports*
  the validation error instead of raising — it is a read-only surface, unlike
  `start`/`resume`, which keep failing the batch closed. The human-readable
  `status` also gains a goal summary line.

### Changed
- **`/redteam:goal` is now an autonomous run-to-completion driver.** Instead of
  stopping after one `start`, the command keeps operating: it reads
  `status --json`, diagnoses deferred/failed tasks, applies the remediations an
  operator agent may make (transient infra fixes, deleting a stale same-named
  task branch, resetting a sticky `next_phase: "deferred"` after addressing the
  recorded cause, amending a defective decomposer-written brief within
  `goal.md`'s intent), and resumes — until every task's draft PR is open or a
  genuinely-human decision is needed. Hard stops are fail-closed: a rejected
  decomposition, the same task deferring twice for the same reason, anything
  touching a security boundary, or ~10 passes without completion. It never
  merges — the draft-PR stack remains the human checkpoint.

### Docs
- Sync drift found in a repo sweep: `commands/install.md` now says seven agent
  skeletons (was "six"; names `goal-decomposer`), `commands/config.md` cites the
  shipped planner default (`claude-opus-4-7`), the orchestrator module
  docstring's usage block lists all eight subcommands, and the READMEs (EN+KO)
  document `status --json` and `wait-and-resume`.

## [0.7.0] - 2026-06-30

The Claude Code plugin's command surface is reworked to match the engine. Slash
commands lose the redundant `redteam-` prefix (`/redteam:install`, not
`/redteam:redteam-install`), and three commands that were only reachable from the
engine CLI — running, resuming, and goal-decomposing a batch — are now exposed,
so the plugin can drive a batch end-to-end, not just scaffold one. The picker now
maps ~1:1 to the orchestrator subcommands. The prefix drop is **breaking** for
anyone who scripted the old names; after updating the plugin, run `/reload-plugins`
or restart.

### Added
- **Three new slash commands surface the rest of the engine: `/redteam:goal`,
  `/redteam:start`, `/redteam:resume`.** `goal` drives goal mode end-to-end
  (`decompose` the batch's `goal.md`, then `start` the validated stack); `start`
  runs a seeded batch through the pipeline for the first time; `resume` continues
  an in-progress batch after a gate, failure, or deferral. Previously only the
  engine CLI exposed `start`/`resume`/`decompose` — the plugin had no way to run
  or continue a batch. The command surface now maps ~1:1 to the orchestrator
  subcommands (install · new-task · goal · start · resume · status · review · config).

### Changed
- **Slash commands dropped the redundant `redteam-` prefix.** Plugin commands are
  already namespaced under the plugin (`/redteam:…`), so the command files were
  renamed `redteam-install.md` → `install.md`, etc. — the invocations are now
  `/redteam:install`, `/redteam:review`, `/redteam:config`, `/redteam:status`,
  `/redteam:new-task` (was `/redteam:redteam-install`, …). Typing `/redteam`
  filters the picker to the eight subcommands. **Breaking** for anyone who scripted
  the old `/redteam:redteam-*` names. The `redteam-install` PATH executable
  (`bin/redteam-install`) is unchanged. (After updating the plugin, run
  `/reload-plugins` or restart to pick up the new names.)

### Docs
- **Goal mode is now documented in the README** (EN + KO) — `decompose` →
  single-parent DAG → parent-first stacked draft PRs, with the fail-closed guard
  rails spelled out (multi-parent rejected in v1, `ceilings.max_tasks` abort,
  frozen parent-tip, `blocked_on_dependency` descendants). Closes the operator-doc
  gap under #94. Also corrects the sub-agent count (six → seven, after the
  goal-decomposer agent landed).

## [0.6.0] - 2026-06-30

**Goal mode** lands: the harness can now take a single human-authored `goal.md`,
decompose it into a dependency-ordered set of tasks, and drive them through the
agent-pair pipeline as one composed run — each dependent task stacked on its
parent's branch, with fail-closed guards at every seam. Alongside it, a family of
integrity-hardening fixes closes the implement commit boundary (the worker's
tracked/untracked baselines and the out-of-scope floors), config gets a
deterministic per-role model picker, and the worker/review surfaces get a batch of
robustness and quality fixes. As always, every change landed through the harness's
own cross-provider adversarial review (Claude implementer ↔ Codex reviewer), and
goal mode itself was validated end-to-end by dogfooding the harness on the very
task of testing goal mode.

### Added
- **Goal mode — multi-task composition from a single goal** (#94). A new
  `orchestrator decompose <batch>` turns a human `goal.md` into a **single-parent
  DAG manifest** (`goal.json`) plus one `input.md` per task, via a `goal-decomposer`
  sub-agent whose output is checked by a **cross-provider decomposition review**
  before any task is seeded. The scheduler then runs tasks **parent-first**, and a
  dependent task **stacks on its parent**: its reviewed range, PR base, and
  changed-paths are pinned to the parent's branch (`parent-branch...HEAD`), so each
  PR shows exactly that task's delta and the stack merges parent-first. Guard rails
  are fail-closed throughout — a **≥2-dependency (multi-parent) manifest is rejected**
  in v1; `ceilings.max_tasks` must match the manifest or the **whole batch aborts
  before any seeding**; a **moved parent-tip / wrong reused base fails closed** via a
  centralized freeze guard (`base_branch_sha` recorded at pin time); a deferred or
  failed parent leaves its descendants `blocked_on_dependency` (skip-and-continue,
  no auto re-plan in v1). Design recorded after a 3-round `plan_review`
  (`docs/decisions/2026-06-27-goal-mode-design.md`, #110); engine in #111 (Slice A:
  manifest + task-on-task branching) and #115 (Slice C: ceilings + done-criterion,
  Slice B: decomposer); end-to-end composition tests in #123 (happy-path + shared
  scaffolding) and #126 (failure-path).
- **Deterministic per-role model picker + `config` subcommand** (#95). The model
  for each role (planner / implementer / reviewer / rescue) is resolved
  deterministically from the active tier profile, and `orchestrator config` surfaces
  the resolved wiring so an operator can see exactly which model each role will use
  before a run (#105, #106).
- **Output-validity / anti-degeneracy check in the review mandate** (#97). The
  reviewer is now required to reject degenerate or empty-shell output (e.g. a "pass"
  that deletes the assertions, or a stub that satisfies the letter of the
  done-criteria without the behavior), closing a gap where a vacuous diff could be
  rubber-stamped (#107).

### Changed
- **Round-over-round reviewer context is narrowed for carried-over findings**
  (#119). On a re-review the reviewer is shown the prior round's still-open findings
  scoped to what carried over, rather than the full prior transcript — less noise,
  tighter convergence on the unresolved items.

### Fixed
- **Implement commit-boundary integrity — the tracked/untracked baseline family.**
  A connected set of fail-closed fixes so the implementer's commit can never sweep
  in (or be tricked into hiding) files outside the task's scope:
  - **Reviewed-range `base_branch` is pinned end-to-end** (#91) — Part B pins it
    across the whole pipeline (#108) and Part A attributes tracked changes to the
    worker against that pinned base (#121), so the review range can't drift with
    live config.
  - **Untracked baseline is snapshotted once per task** (#112) — the pre-worker
    untracked surface is set-once, closing an in-flight migration/TOCTOU window
    (#116).
  - **Cross-run trust-root floor** (#117) — on a cross-run resume, both the live
    outside-scope untracked surface AND the stored baseline contents must be clean
    before the worker is invoked; either probe failing fails closed, catching both
    the "leave-on-disk" and "future-create" variants of a tampered baseline (#122).
  - **Sibling-task artifacts are exempt from the out-of-scope tracked floor** (#124)
    — a stacked dependent no longer defers over a sibling task's harness-owned
    decision-trail files (`state.json` / `outcome.md` / `pr.md` / `*_review.md`),
    while arbitrary sibling paths, sibling subdirectories, and cross-batch paths
    still trip the floor (#125).
  - **TDD/agent-pair commit discipline** (#82) — agent-pair `implement` commits
    newly-created untracked files (#83); the TDD reviewer sees the work via an
    untracked-inclusive review patch (#89); per-phase commit + manifest discipline
    across phases (#93).
- **Hard wall-clock deadline in the Claude worker** (#109) — a worker phase that
  hangs is bounded by an enforced deadline instead of running unbounded (#118).
- **Bounded rescue-entry route** (#87) — the rescue path defers to a human after a
  bounded number of attempts instead of looping indefinitely (#88).
- **Mode-aware skeletons + `create_pr` prompt** (#73) — no TDD assumptions leak
  into the agent-pair flow; the sub-agent skeletons and the PR prompt adapt to the
  active mode (#84).
- **Worker permission mode + allowed tools are configurable** (#99) — the engine no
  longer hard-codes the worker's permission mode / tool allowlist (#100).
- **Standalone `review` suspends the pipeline-only verification gate** (#103) — the
  one-shot `review` subcommand no longer trips a gate meant only for the full
  pipeline (#104).
- **Each review round's full text is preserved, round-numbered** (#86) — earlier
  rounds are no longer overwritten, so the decision trail keeps every round (#90).

### Docs
- **README: Korean translation + bidirectional language links** (#96).
- **README: Origin + "Why cross-review" sections** (model-independent framing)
  (#102).
- **"Codex main, Claude sub" configuration example** added to the Model-freedom
  docs (#81).

### Internal
- **Bump `actions/checkout` 6 → 7** in the actions group (#98).
- **Pin Codex-main wiring in tests** (codex worker / claude reviewer resolution)
  (#85).

## [0.5.1] - 2026-06-19

### Fixed
- **Subagent tool restrictions now actually apply** (#76). All six bundled
  `.claude/agents/*.md` declared their tool allowlist with `allowed-tools:` — the
  slash-command/settings key, which Claude Code **silently ignores** on a
  subagent, so each agent inherited the parent's full tool set (a read-only
  reviewer could Edit/Write). Switched to the documented subagent key `tools:`,
  and corrected the lists to what each role needs (added `Write` to
  `outcome-planner` and the two reviewers, which produce an output file — a bare
  rename would otherwise have broken them). Pinned in a new test. Consumers should
  re-vendor (`redteam-install . --overwrite`). Sandbox-enforced read-only for the
  live adversarial reviewer remains the headless adapter path
  (`codex --sandbox read-only` / `claude --permission-mode plan`); this is the
  restriction for the in-session sub-agent path.

## [0.5.0] - 2026-06-19

The default common path is now genuinely gateless — the code matches what the docs
always promised — and the agent-pair/TDD flow is described accurately throughout.
Surfaced by a downstream consumer (the `portfolio` project) and driven through the
harness's own cross-provider review (`plan_review` + `review_code`).

### Changed
- **The default common path is gateless, matching the documented design** (#71,
  #75). The static `agent-pair` and `tdd` phase orders no longer carry the
  `human_gate_outcome` (plan-approval) gate — the adversarial pair + verify is the
  automated trust and the draft PR is the human checkpoint, exactly as the README
  describes ("no human gates in the common path"). Previously the static orders
  blocked at `human_gate_outcome`, contradicting the docs. A plan-approval gate is
  still **opt-in per tier profile** (`gates = ["outcome"]`), unchanged. **Behavior
  change** (removes a default gate) → this minor release. A task persisted while
  parked at the old gate migrates forward on resume (agent-pair → `implement`, tdd
  → `write_test`) instead of silently finishing.
- **Docs no longer call the default agent-pair flow "test-first"** (#71, #72, #77).
  Agent-pair is `plan → implement → review` (the worker writes tests inside
  `implement`); `write_test`/`verify_test` and the test-author / test-verifier
  sub-agents are **TDD-mode only**. Adds a "Phases by mode" table, fixes the
  Mermaid and the role/gate descriptions, and labels the TDD-only skeletons — so a
  manual pipeline driver can't be led into running a phase the mode excludes.

## [0.4.0] - 2026-06-17

A small, focused release cut at a clean stopping point: the task-scaffolding
command and the consumer-facing `verify.sh` seed are done and tested, and the
roadmap is empty (no open issues), so there is nothing in flight to wait for.
This also closes the #37 reviewer-transport line of work entirely — only its
fallback-ladder step ever shipped (in 0.3.0); the other two were rejected (docs).

### Added
- **Task-scaffolding command** (#55) — `orchestrator.py new <batch-dir> <slug>
  [--title]` and `/redteam:redteam-new-task` create the next `task-NNN` directory
  and seed `input.md` from a template, so the brief the planner reads can't be
  subtly malformed.

### Fixed
- **`verify.sh` seeds from a generic, fail-closed template** (#43) — a consumer no
  longer inherits redteam's own gate; an unconfigured `verify.sh` fails closed
  until the consumer sets their stack's checks.
- **Green test suite on a Windows/non-UTF-8 (cp949) host** (#48) — test-side
  `read_text()` calls pin `encoding="utf-8"` and POSIX exec-bit assertions are
  guarded for non-Windows. Tests only; no engine change.

### Changed
- **Reviewer-transport design decisions** (#37, #67) — documented rejection of a
  terminal-multiplexer screen-scraping transport (step 6) and, after weighing the
  options, rejection of the sub-agent reviewer adapter (step 5) as well: the
  headless `claude -p` reviewer already covers the Claude-reviewer case
  cross-provider, so the marginal in-session-steering gain does not justify a
  second execution surface plus the family-vs-key normalization prerequisite. #67
  closed. See `docs/decisions/`. No engine change.

## [0.3.0] - 2026-06-16

A reliability-and-resilience release: the harness gets the fail-closed
backstops, the update-visibility tooling, and the operator surfaces that the
0.2.0 features revealed it needed. Every change landed through the harness's own
cross-provider adversarial review (Codex reviewing Claude-written code), which
caught four real HIGH-severity defects before merge.

### Added
- **Reviewer fallback ladder** (#37 step 4). When the primary headless reviewer
  fails on *infrastructure* (missing CLI / auth / timeout / unparseable output),
  the engine applies a configurable, fail-closed `reviewer_fallback` ladder. A
  valid review decision (incl. `CHANGES_REQUESTED`) is never a fallback trigger; a
  fallback's `APPROVED` is trusted only if it is cross-provider, read-only, and
  cleanly parsed; otherwise the task blocks for a manual review. Default
  `reviewer_fallback = "manual"` (fail-closed). Structured, un-spoofable audit
  trail.
- **Install version stamp + `redteam-install --check`** (#34). Vendoring writes a
  `.redteam/.redteam-version` stamp; `--check` reports whether a consumer's
  vendored harness is behind the source (exit 0/1/2) without writing anything.
- **Dispatch-time pre-implement snapshot invariant** (#39). A single fail-closed
  backstop guarantees the verification snapshot (`verify_command` + allowlist +
  commands) is fully pinned before the implementer can mutate the tree — no path
  can reach `implement` unpinned.
- **Per-task operator `progress.md` surface** (#49). A best-effort, secret-safe
  human-readable status mirror for long / detached runs; gitignored and never
  committed into a PR.

### Fixed
- **`cmd_review` provider resolution converged + fail-closed config load** (#40).
  The standalone `review` now uses the same provider resolvers as the in-pipeline
  guard (one source of truth) and exits 2 on a malformed config instead of
  tracebacking.
- **`implement` fails closed on uncommitted scope changes** (#50). If the scoped
  commit leaves source/test files uncommitted, the reviewed range would be stale;
  the phase now fails closed instead of handing review a stale range.
- **`create_pr` preflights PR auth** (#51). A headless run with `gh` missing /
  unauthenticated now fails closed with a remedy instead of stalling on an
  interactive prompt until the worker timeout.
- **Order-independent test suite** (#54). Engine modules are loaded once and
  shared in tests, removing an order-dependent isolation flake.

### Changed
- Documented the `/redteam` commands, the `review` subcommand, and the installer
  flags in the README (#53).

## [0.2.0] - 2026-06-14

### Added
- **Fail-closed guard against same-provider self-review** (#28). The orchestrator
  refuses to run when the configured headless reviewer resolves to the same
  provider family as the worker — the adversarial pair must stay cross-provider.
  Surfaced both in-pipeline and via the standalone `review` command.
- **`/redteam` slash commands + standalone `review` subcommand** (#29).
  `orchestrator.py review` runs a one-shot, read-only adversarial review of the
  current branch diff with the configured reviewer (fail-closed exit codes:
  0=APPROVED, 1=changes, 2=reviewer failed / self-review). `/redteam:review`,
  `/redteam:config`, `/redteam:status`; `/config` enforces the cross-provider
  invariant.
- **Opt-in `--protect-config` installer flag** (#30). When passed, `install.py`
  merges add-only Edit/Write deny rules for `.redteam/config.toml` into a
  consumer's `.claude/settings.json` (never clobbers; off by default — the runtime
  pairing guard is the backstop).
- **Pipeline-mode validation and selection** (#36). `mode` (agent-pair vs tdd) is
  validated against an explicit enum and fails closed on an unknown value (no more
  silent TDD fall-through on a typo); a fresh task can select it via `input.md`
  front-matter (`+++ mode = "tdd" +++`), reconciled with tier routing.

### Fixed
- **Verification snapshot taken at the `ask_user` APPROVE → implement hand-off**
  (#35, HIGH). A plan-review escalation answered APPROVE previously ran implement
  with no snapshotted verify commands and was falsely deferred; now the snapshot
  is taken fail-closed.
- **UTF-8 pinned across all engine subprocess decode paths** (#32). Adapters and
  every git/gh/verify text capture now pass `encoding="utf-8"`, fixing crashes on
  non-UTF-8 platforms (e.g. cp949 on Korean Windows) when output contained
  non-ASCII characters.
- **`verify.sh` activates the Windows venv layout** (#33) — `venv/Scripts/activate`
  in addition to the POSIX `venv/bin/activate`.
- **Dogfood reviewer docs filled + consumer docs seeded from templates** (#31).
  `.redteam/docs/*` now describe redteam's real rules; consumer installs seed the
  generic skeletons from `.redteam/templates/docs/` instead of leaking redteam's
  own docs.

## [0.1.0]

Initial public release: the standalone, Apache-2.0 redteam harness extracted from
its origin monorepo — stdlib-only engine, prompts, agent skeletons, installer, and
Claude Code plugin packaging, with tier-aware routing (#13).

[Unreleased]: https://github.com/AscendyProject/redteam/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/AscendyProject/redteam/compare/v0.8.0...v0.9.0
[0.5.1]: https://github.com/AscendyProject/redteam/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/AscendyProject/redteam/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AscendyProject/redteam/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.3.0
[0.2.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.2.0
[0.1.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.1.0
