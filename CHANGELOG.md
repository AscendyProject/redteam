# Changelog

All notable changes to the redteam harness are recorded here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
releases may include behavior changes; breaking changes are called out).

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

[0.2.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.2.0
[0.1.0]: https://github.com/AscendyProject/redteam/releases/tag/v0.1.0
