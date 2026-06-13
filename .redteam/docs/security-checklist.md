# Security checklist — redteam

> The code-security-reviewer sub-agent applies this list to every diff (path from
> `config.toml [project] security_checklist`). Each item is a **hard line**: a
> single confirmed HIT escalates to `REVIEW_DECISION: CHANGES_REQUESTED`. This
> repo **dogfoods its own harness**, so the items below are redteam's real
> boundaries (a stdlib-only CLI orchestrator), not the web/DB/auth checklist a
> typical app needs — for that shape see
> `examples/fastapi-like/.redteam/docs/security-checklist.md`.

Don't relax the checklist for "small" changes; small diffs are where regressions hide.

## 1. Verification allowlist & snapshot (trust boundary)
- [ ] **The verify allowlist stays exact-argv-trusted.** `verify_command` is
      trusted as-is; everything else must be in `verification_allowlist`
      (`phase_runners/_base.py validate_verification_commands`). No widening to
      free-form / shell-evaluated commands.
- [ ] **The plan-time snapshot is intact.** `verify_command` + allowlist are
      snapshotted before implement so an implementer can't widen them mid-round
      (IR-001). Any new path to implement must take or inherit that snapshot.
- [ ] **Fail-closed preserved.** A missing/invalid snapshot defers the task; it
      must never let implement run unpinned or report a false approval.

## 2. Installer file-class split (data-loss boundary)
- [ ] **No clobber of consumer-owned files.** `scripts/install.py`: harness-owned
      trees are replaceable, agent skeletons are copied file-by-file, project-
      owned seeds (`config.toml`, `docs/*`, `verify.sh`, `batches/`) are written
      once and never overwritten — even with `--overwrite`.
- [ ] **No `rmtree`/delete of a path a consumer co-owns.** Mutating
      `.claude/settings.json` is add-only/idempotent and opt-in
      (`--protect-config`).

## 3. Adapter trust model
- [ ] **Reviewer runs read-only.** Reviewer adapters use `--sandbox read-only` /
      `--permission-mode plan` and must not gain write/edit capability. Only the
      worker adapter mutates the workspace.
- [ ] **No credential / secret leak via adapter output.** Raw stderr containing
      tokens/keys must not be surfaced into logs, feedback, or review text.

## 4. Adversarial pairing (anti-self-review)
- [ ] **Cross-provider enforced fail-closed.** The reviewer must resolve to a
      different provider than the worker; a same-provider pairing (in-pipeline
      guard or standalone `cmd_review`) must defer/exit non-zero, never approve.

## 5. Subprocess & encoding safety
- [ ] **Shell-free.** No `shell=True`; pass arg lists. No interpolation of
      untrusted strings into a command line.
- [ ] **Text-mode captures pin `encoding="utf-8"`** so non-ASCII output can't
      crash on a non-UTF-8 platform default (cp949). Bytes-mode calls stay bytes.

## 6. Project-agnosticism & zero deps (integrity boundary)
- [ ] **No project/stack fingerprints** leak into `.redteam/workflows/` or
      non-example tests. The config seam is the only home for project specifics.
- [ ] **No new non-stdlib import in the engine** without explicit justification —
      it breaks the vendor-and-run promise.

## 7. Tooling
- [ ] **`bash .redteam/scripts/verify.sh` passes** (ruff + pytest). A read-only
      sandbox that can't run it must say so and rely on the reported result.
- [ ] **No weakening of tests / types / lint** to pass CI (`# type: ignore`,
      `# noqa`, `xfail`, `skip`, deleted assertions) without an explicit
      justification per line.

## 8. Secrets
- [ ] **No secrets in commits or logs** (`.env*`, key/cert files, tokens).
- [ ] **No license-direction change or CLA weakening** without the operator's
      explicit decision.
