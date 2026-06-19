# Changelog

All notable changes to the redteam harness are recorded here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
releases may include behavior changes; breaking changes are called out).

## [Unreleased]

## [0.5.0] - 2026-06-19

The default common path is now genuinely gateless — the code matches what the docs
always promised — and the agent-pair/TDD flow is described accurately throughout.
Surfaced by a downstream consumer (the `portfolio` project) and driven through the
harness's own cross-provider review (plan_review + code_review).

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

[Unreleased]: https://github.com/AscendyProject/redteam/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/AscendyProject/redteam/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AscendyProject/redteam/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.3.0
[0.2.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.2.0
[0.1.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.1.0
