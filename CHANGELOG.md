# Changelog

All notable changes to the redteam harness are recorded here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
releases may include behavior changes; breaking changes are called out).

## [Unreleased]

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

[Unreleased]: https://github.com/AscendyProject/redteam/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/AscendyProject/redteam/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/AscendyProject/redteam/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AscendyProject/redteam/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.3.0
[0.2.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.2.0
[0.1.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.1.0
