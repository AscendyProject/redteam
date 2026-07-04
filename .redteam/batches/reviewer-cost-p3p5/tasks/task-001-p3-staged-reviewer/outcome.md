# Outcome — P3: Round-staged reviewer model

## Goal
Let an operator declare a cheaper first-pass reviewer that handles the early
`review_code` rounds and escalates to the configured frontier reviewer on later
rounds, while (a) keeping the default behavior byte-identical when the new
config is absent, (b) preserving the cross-provider adversarial-pairing guard
against BOTH the first-pass reviewer AND the frontier reviewer, and (c)
guaranteeing that no round dispatched to the first-pass reviewer can finalize
as `PhaseResult.status == "approved"`.

## Design decisions (fixed here; the implementer must not re-negotiate)

Each decision closes a specific blocker from the prior plan_review round.
Tests are written against them.

### D1. Config namespace — nested `[models.review_stages]` GLOBAL subtable, GLOBAL-ONLY

`[models.review_stages]` is a NEW nested subtable under the existing top-level
`[models]` block. It has exactly two keys, both required when the subtable is
present, unknown keys rejected fail-loud with the same
`Unknown ... config key(s)` shape as `_build`:

- `first_pass_reviewer: str` — must be a key of `adapters._REVIEWER_ADAPTERS`
  (today: `"codex"` or `"claude"`). Manual sentinels (`"manual"`, `"human"`)
  are rejected — a manual first-pass defeats P3's cost-cutting purpose.
- `escalate_after: int` — must satisfy `int >= 1` (Python `bool` values
  rejected, mirroring `_parse_triggers`'s bool guard). Semantics:
  rounds `1..escalate_after` use the first-pass reviewer; round
  `escalate_after + 1` and onward use the configured frontier reviewer.

Absence of the whole subtable is the default — no staging, today's behavior
byte-for-byte.

**Tier-level staging is EXPLICITLY OUT OF SCOPE for v1** (operator decision).
Staging is configured only under the top-level `[models]`. This task does NOT
change the `TierProfile.models` type contract (`dict[str, str]`), does NOT
change `_parse_tiers`'s per-role `str`-value enforcement, and does NOT accept
a `review_stages` key inside `[tiers.N].models`. A `[tiers.N].models` table
that contains a `review_stages` key continues to be rejected by today's
"unknown role(s)" fail-loud path (a `review_stages` value that is a dict
also fails today's `isinstance(model, str)` check — both paths keep working
as-is, no new code needed for the rejection).

### D2. Round-number source — `state["implement_round_count"]`

This counter is set by `phase_runners.implement._commit_wip_round`
(`.redteam/workflows/phase_runners/implement.py:308-309`) immediately before
`review_code` runs in the agent-pair pipeline, so at `review_code.run` entry,
`int(state.get("implement_round_count") or 0) == N` means "this is
review-code round N". It is already persisted, monotonic across resumes, and
survives rescue → implement backtracks (rescue lands in implement, which
increments it). Legacy state without the counter treats it as `0` and the
first `review_code` after implement always sees the incremented counter.

### D3. Cheap-APPROVED handling — in-runner promotion to the frontier for the SAME round (strategy (a) from `input.md:42-47`)

If a round was dispatched to the first-pass reviewer AND the parsed verdict
is `APPROVED` with `parse_status == "ok"`, `phase_runners.review_code.run`
MUST immediately invoke a second review through the FRONTIER reviewer with
the same `role`, `cwd`, and `target`, and return THAT frontier result to the
orchestrator. The in-round promotion is bounded (exactly one extra invocation
per round). All other first-pass outcomes (`CHANGES_REQUESTED`,
`RESCUE_REQUIRED`, `ASK_USER`, any `parse_status != "ok"`, `MANUAL_REQUIRED`)
are returned as-is — that is the cost-saving that motivates P3.

Consequence: `PhaseResult.status == "approved"` can only be produced by a
frontier-resolved reviewer invocation.

### D4. Data path — global-only, read from live config at dispatch time

Staging config is READ ONLY via `load_config(repo_root()).models.review_stages`
at dispatch time inside the runner, mirroring the existing lazy-live-config
pattern (`phase_runners/_base.py::default_model_for_role`,
`phase_runners/_base.py::project_config`,
`phase_runners/_base.py::validate_verification_commands` fall-through).

- `state.template.json` is UNCHANGED (no new keys).
- `state["models"]` is UNCHANGED (no `review_stages` sub-key ever written).
- The tier-merge code at `orchestrator.py:1213-1214` is UNCHANGED and continues
  to merge `dict[str, str]` per-role overrides only.
- The frontier reviewer identity continues to come from `reviewer_provider(state)`
  (which reads `state["models"]["reviewer"]` with a config fallback), preserving
  today's per-task reviewer override.
- The first-pass reviewer identity is a GLOBAL config value only; it is NOT
  overridable per-task in v1.

### D5. Dispatch contract — ONE unambiguous decision tree, three named function boundaries

`phase_runners.review_code.run` (agent-pair branch only) resolves each round
into exactly ONE of four dispatch cases. This is the entire dispatch contract
— no other case exists.

**Case A — Manual/human reviewer.** Determined by
`get_reviewer_adapter(state) is None` (equivalently:
`reviewer_provider(state) is None`), OR by
`"review_code" in state.get("manual_review_required", {})` (a prior fallback
exhausted to manual for this phase). Staging is BYPASSED ENTIRELY. Runner
takes the existing manual branch: reads `code_review.md` from disk, parses
`REVIEW_DECISION:`, returns as today. No `review_with_fallback*` call.
UNCHANGED from today's behavior.

**Case B — Staging OFF, headless reviewer.** Determined by
`get_reviewer_adapter(state) is not None` AND
`load_config(repo_root()).models.review_stages is None`. Runner invokes
`adapters.review_with_fallback(state, role, prompt, cwd, target)` exactly as
today. The wrapper `review_with_fallback_for_provider` is NOT called. This is
BYTE-IDENTICAL to today's behavior (same function, same signature, same
prompt, same audit trail).

**Case C — Staging ON, frontier round.** Determined by
`get_reviewer_adapter(state) is not None` AND
`review_stages is not None` AND
`int(state.get("implement_round_count") or 0) > review_stages.escalate_after`.
Runner invokes `adapters.review_with_fallback(state, role, prompt, cwd, target)`
— the same function boundary as Case B. Rationale: the primary is already the
state-default (frontier) reviewer, so there is no reason to route through the
`_for_provider` variant; using the exact same function keeps this case's audit
trail and behavior identical to Case B and to today's approved-frontier path.

**Case D — Staging ON, first-pass round.** Determined by
`get_reviewer_adapter(state) is not None` AND
`review_stages is not None` AND
`int(state.get("implement_round_count") or 0) <= review_stages.escalate_after`.
Runner invokes
`adapters.review_with_fallback_for_provider(state, role, prompt, cwd, target,
primary_provider=review_stages.first_pass_reviewer)`. If the returned
`ReviewResult` has `parse_status == "ok"` AND `decision == "APPROVED"`, the
runner IMMEDIATELY invokes `adapters.review_with_fallback(state, role, prompt,
cwd, target)` (same function as Case B/C) and RETURNS THAT SECOND RESULT
mapped to `PhaseResult` via the same decision→status table. Any other
first-pass result is returned as-is (a fail-closed guard also rejects any
future-refactor path that would let a first-pass result exit through the
approved branch).

Function-boundary summary (one-line-per-case):
- Case A → runner never calls `review_with_fallback*`; manual branch as today.
- Case B → `review_with_fallback(state, ...)` (today's signature; byte-identical).
- Case C → `review_with_fallback(state, ...)` (same as Case B).
- Case D → `review_with_fallback_for_provider(state, ..., primary_provider=<first_pass>)`,
  plus a bounded second `review_with_fallback(state, ...)` on cheap-APPROVED promotion.

### D6. New adapter registry helpers

`.redteam/workflows/adapters/__init__.py` gains exactly two new public names,
both re-exported via `__all__`:

- `get_reviewer_adapter_by_provider(name: str) -> ReviewerAdapter | None` —
  returns a reviewer adapter for the given provider key (`"codex"` /
  `"claude"`), or `None` if `name` is not a registered adapter key. Reuses
  `_REVIEWER_ADAPTERS`; is the SOLE path to instantiate a stage's adapter.
- `review_with_fallback_for_provider(state, *, role, prompt, cwd, target,
  primary_provider: str) -> ReviewResult` — runs the fallback ladder against
  an EXPLICITLY-CHOSEN primary provider instead of the state-default one.
  Semantics mirror `review_with_fallback` verbatim except that `primary` is
  `get_reviewer_adapter_by_provider(primary_provider)` and `primary_name` is
  `primary_provider`. Fallback selection, the `fb == worker_provider(state)`
  check, `read_only_enforced`, `MANUAL_REQUIRED` behavior, and the
  `fallback_audit` field are BIT-FOR-BIT the same as the original function.

To guarantee bit-for-bit parity, `review_with_fallback` and
`review_with_fallback_for_provider` share a single private implementation
(a `_review_with_fallback_impl(state, *, role, prompt, cwd, target, primary,
primary_name) -> ReviewResult` helper). `review_with_fallback` delegates by
passing `primary=get_reviewer_adapter(state)` and
`primary_name=reviewer_provider(state) or primary.name`;
`review_with_fallback_for_provider` delegates by passing
`primary=get_reviewer_adapter_by_provider(primary_provider)` and
`primary_name=primary_provider`. The two callers cannot drift.

If `get_reviewer_adapter_by_provider(primary_provider)` returns `None`
(the configured first-pass identifier is somehow missing from the registry
at dispatch time), `review_with_fallback_for_provider` returns
`MANUAL_REQUIRED` with an audit body naming the mismatch (mirrors today's
"no headless reviewer adapter configured" branch).

### D7. First-pass raw preservation across rounds

On a promoted round, the runner writes the first-pass reviewer's raw to
`task_dir / "code_review.first_pass.md"` BEFORE issuing the frontier
invocation, so a crash between the two calls does not lose the first-pass
audit trail.

To rotate the first-pass artifact alongside `code_review.md`,
`orchestrator._clear_manual_phase_artifacts` (`.redteam/workflows/orchestrator.py:524`)
gains an EXPLICIT second `_archive_review_round(task_dir,
"code_review.first_pass.md")` call in its `review_code` branch (right after
the existing `_archive_review_round(task_dir, "code_review.md")`). The
mapping is expanded from a single filename to a small tuple for the
`review_code` phase; `plan_review` and `rescue` continue to archive a single
file. Resume-safe: `_archive_review_round` no-ops when the file is absent, so
non-promoted rounds pay nothing.

### D8. Cross-provider guard extension (defensive; scoped to review_code staging)

`orchestrator._adversarial_pairing_error` (`.redteam/workflows/orchestrator.py:292`)
gains one additional check that fires ONLY when BOTH conditions hold:
(i) staging is configured (`load_config(repo_root()).models.review_stages
is not None`) AND (ii) `"review_code" in _phase_order(state)` AND
`_mode(state) == "agent-pair"`. Under those conditions, the guard
additionally returns an error if
`review_stages.first_pass_reviewer == worker_provider(state)`, naming that
the FIRST-PASS side collapsed to self-review. The existing
`runs_headless_review` and frontier-side check are UNCHANGED — the new check
is strictly additive, and the error message identifies which side collapsed.

## Done-when
- [ ] `.redteam/workflows/config.py` gains a frozen dataclass
      `ReviewStagesConfig(first_pass_reviewer: str, escalate_after: int)`
      and `ModelsConfig` gains an optional field
      `review_stages: ReviewStagesConfig | None = None`. Unknown keys inside
      `[models.review_stages]` raise `ValueError` with the same
      `Unknown ... config key(s)` shape as `_build`.
- [ ] `config.load_config` fails loud with `ValueError` on: (a)
      `first_pass_reviewer` not a key of `adapters._REVIEWER_ADAPTERS`;
      (b) `first_pass_reviewer` equal to `"manual"` / `"human"`;
      (c) `escalate_after` that is not an `int >= 1` (Python `bool` values
      rejected); (d) either required key missing when the subtable is
      present; (e) either key of the wrong TOML type.
- [ ] `_parse_tiers` is UNCHANGED. `TierProfile.models` stays
      `dict[str, str]`. A `[tiers.N].models.review_stages` sub-table continues
      to be rejected by today's fail-loud paths (unknown-role OR non-str
      value). A regression test asserts both rejection paths.
- [ ] With `[models.review_stages]` absent from `.redteam/config.toml`,
      `load_config(repo_root)` returns a `RedteamConfig` whose
      `models.review_stages is None`, and every other `ModelsConfig` field
      is byte-identical to today's shipped defaults. Regression-locked.
- [ ] `.redteam/workflows/adapters/__init__.py` gains exactly two new
      public names, re-exported via `__all__`:
      (i) `get_reviewer_adapter_by_provider(name: str) -> ReviewerAdapter | None`;
      (ii) `review_with_fallback_for_provider(state, *, role, prompt, cwd,
      target, primary_provider: str) -> ReviewResult`.
      Both are implemented by delegating to a single private
      `_review_with_fallback_impl` shared with `review_with_fallback`.
- [ ] `review_with_fallback`'s public signature and observable semantics are
      UNCHANGED. Every existing caller (`plan_review.run`, and Case B / Case C
      of `review_code.run`) continues to invoke it exactly as today.
- [ ] `phase_runners/review_code.py::run` (agent-pair branch ONLY)
      implements the D5 dispatch contract with a single private helper
      `_resolve_round_stage(state) -> Literal["manual", "unstaged", "frontier", "first_pass"]`
      (or an equivalent tagged enum). The dispatch tree matches Cases A / B /
      C / D exactly. The runner contains NO hardcoded provider literal
      (`"codex"` / `"claude"`); every provider identifier flows in from
      `state["models"]` / `load_config()` output.
- [ ] With `load_config(repo_root()).models.review_stages is None`,
      `review_code.run` is byte-identical to today: same prompt build
      (full or narrowed), one `review_with_fallback` call, same
      `PhaseResult` shape, no `code_review.first_pass.md` file. Regression-locked.
- [ ] With staging ON and the round dispatched to the first-pass reviewer
      AND the parsed verdict is `APPROVED` with `parse_status == "ok"`,
      `review_code.run` MUST invoke `review_with_fallback` a second time
      within the same `run()` call and return THAT result. No code path
      exists by which a first-pass `ReviewResult(decision="APPROVED")` maps
      to `PhaseResult(status="approved")`.
- [ ] `code_review.md` after a promoted round contains the FRONTIER
      reviewer's raw. The first-pass reviewer's raw is written to
      `task_dir / "code_review.first_pass.md"` BEFORE the frontier
      invocation, and rotates through `code_review.first_pass.round1.md`,
      `code_review.first_pass.round2.md`, … on subsequent rounds via
      `_archive_review_round`.
- [ ] `orchestrator._clear_manual_phase_artifacts` (`.redteam/workflows/orchestrator.py:524-533`)
      is extended so that when `phase == "review_code"` it makes TWO
      `_archive_review_round` calls: the existing one on `code_review.md`
      AND an explicit second call on `code_review.first_pass.md`. The
      `plan_review` and `rescue` mappings are UNCHANGED (single file each).
      Resolves the prior "Uncertain" comment.
- [ ] `.redteam/workflows/phase_runners/_base.py::PhaseResult` gains an
      optional field `staging_audit: NotRequired[str]` — structured
      provenance set by the runner (never parsed from reviewer text),
      mirroring `fallback_audit`. On a promoted round the runner populates
      `staging_audit` with a short string naming the round number and the
      promoted-from provider.
- [ ] `orchestrator._adversarial_pairing_error` (`.redteam/workflows/orchestrator.py:292-330`)
      gains the D8 first-pass check. The check is additive, only fires when
      staging IS configured, and only when `review_code` is in the resolved
      order in agent-pair mode. Error message names which side collapsed.
- [ ] `orchestrator.py`'s existing `review_audit` wiring
      (`.redteam/workflows/orchestrator.py:1448-1450`) additionally appends a
      `{"phase": "review_code", "reason": <staging_audit>}` entry when
      `PhaseResult.staging_audit` is present, mirroring the existing
      `fallback_audit` append. Trusted only when set by the runner
      (structured field), never from in-band reviewer text.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff format
      --check + full pytest under `.redteam/tests/`) — including the new
      test file AND every existing test.
- [ ] `.redteam/config.toml` in THIS repo is UNCHANGED — no
      `[models.review_stages]` subtable is added. Regression-locked by a
      test that loads this repo's own config and asserts
      `cfg.models.review_stages is None`.

## Out of scope
- Any hard ceiling on review-code rounds, wall-clock, or token spend — that
  is task-002 (P5). The nested `[models.review_stages]` namespace is chosen
  so P5 can add ceiling keys without another restructure.
- **Tier-level (`[tiers.N].models.review_stages`) staging.** Operator
  decision at the ask_user gate: staging is GLOBAL-ONLY in v1. This task
  does NOT change `TierProfile.models`'s `dict[str, str]` type contract,
  does NOT change `_parse_tiers`'s per-role `str`-value enforcement, and
  does NOT add any tier-level staging seam. A future task may revisit this.
- P4 (offloading first-pass checks to static analysis / lint) and any
  changes to the native-diff adapter or the #120 context-narrowing coupling.
- The worker / implementer side of the loop (`implement.py`,
  `write_test.py`, `rescue.py`) — no changes.
- Making staging the DEFAULT — it stays opt-in and off in this repo's own
  config.
- Enabling staging for `plan_review` — this task's routing seam is
  `review_code` only. `plan_review.run` continues to call
  `get_reviewer_adapter(state)` + `review_with_fallback(state, ...)` as today.
- Changing `review_with_fallback`'s ladder logic (fallback selection,
  `MANUAL_REQUIRED` semantics, `fallback_audit` provenance). The runner
  chooses WHICH stage's provider to send through the ladder; the ladder
  logic itself is UNCHANGED and shared by both callers via the private
  `_review_with_fallback_impl`.
- Modifying the sub-agent / TDD reviewer tail of `review_code.py`
  (`state.get("mode") != "agent-pair"` path). Staging is agent-pair only.

## Affected files
- `.redteam/workflows/config.py` — add `ReviewStagesConfig`, extend
  `ModelsConfig.review_stages`, parse `[models.review_stages]` in the
  `_build`/`_validate` path with fail-loud validation. `_parse_tiers` is
  UNCHANGED (global-only per D1).
- `.redteam/workflows/adapters/__init__.py` — add
  `get_reviewer_adapter_by_provider` and `review_with_fallback_for_provider`.
  Refactor `review_with_fallback` to delegate to a shared private
  `_review_with_fallback_impl` so the two callers cannot drift. Update
  `__all__`.
- `.redteam/workflows/phase_runners/review_code.py` — add
  `_resolve_round_stage`, route dispatch per D5's four cases in the
  agent-pair branch of `run()`, implement the in-round promotion on
  cheap-APPROVED, write `code_review.first_pass.md` BEFORE the frontier
  invocation, populate `PhaseResult.staging_audit` on promoted rounds.
- `.redteam/workflows/phase_runners/_base.py` — add
  `staging_audit: NotRequired[str]` to `PhaseResult` (mirrors
  `fallback_audit`).
- `.redteam/workflows/orchestrator.py` — extend
  `_adversarial_pairing_error` with D8's first-pass check; extend
  `_clear_manual_phase_artifacts`'s `review_code` branch to make an
  explicit second `_archive_review_round(task_dir,
  "code_review.first_pass.md")` call; extend the `review_audit` wiring
  (`orchestrator.py:1448-1450`) to also append a `staging_audit` entry
  when `PhaseResult.staging_audit` is set.
- `(new) .redteam/tests/test_review_code_staged_reviewer.py` — new tests
  covering D1–D8 behaviors; the test-writing phase (agent-pair implementer)
  defines exact test function names.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

## Verification hooks

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  --check + pytest under `.redteam/tests/`).
- `.redteam/tests/test_config.py` — existing config-loader discipline
  (unknown-key / bad-type rejection) must remain green under the new
  nested subtable.
- `.redteam/tests/test_tier_routing_config.py` — tier parsing behavior
  must be UNCHANGED (this task does not touch `_parse_tiers`).
- `.redteam/tests/test_adversarial_pairing_guard.py` — existing frontier-side
  cross-provider guard tests must remain green; the D8 extension must not
  weaken any existing check.
- `.redteam/tests/test_reviewer_fallback.py`,
  `.redteam/tests/test_reviewer_adapter.py`,
  `.redteam/tests/test_claude_reviewer_adapter.py` — the reviewer adapter
  contract and the fallback ladder must remain green (the wrapper delegates
  to the shared private impl, so ALL existing ladder tests still cover the
  same code paths).
- `.redteam/tests/test_review_round_archive.py` — the existing
  `code_review.md` rotation must remain green; the new
  `code_review.first_pass.md` rotation is additive.
- `.redteam/tests/test_review_code_narrow_context.py` — narrowed-prompt
  behavior must be unchanged with staging OFF and still coherent with
  staging ON (see the narrowed-prompt risk item).
- `.redteam/tests/test_gateless_default_common_path.py` — the default
  common path (no staging) must remain unchanged.

### To be created (the test-writing phase will define exact test function names)
Tests under `.redteam/tests/` in a new file matching `test_*.py` (target
file: `test_review_code_staged_reviewer.py`), covering these behaviors:

- **Config parsing (happy path).** A `[models.review_stages]` table with a
  valid `first_pass_reviewer` and a valid `escalate_after >= 1` loads
  cleanly and is reachable at `cfg.models.review_stages.first_pass_reviewer`
  / `.escalate_after`.
- **Config parsing (fail-loud).** `load_config` raises `ValueError` on each
  of: unknown key inside the subtable; `first_pass_reviewer` not in
  `_REVIEWER_ADAPTERS`; `first_pass_reviewer` set to `"manual"` /
  `"human"`; `escalate_after` that is a `bool`, non-`int`, `0`, or
  negative; either required key missing when the subtable is present.
- **Tier-level staging is REJECTED (global-only invariant).** A config with
  `[tiers.N].models.review_stages = { ... }` (or a bare `review_stages` role
  value) fails loud at load time via the existing per-role rejection path;
  `TierProfile.models` remains `dict[str, str]`.
- **Default parity (no staging).** With
  `load_config(repo_root()).models.review_stages is None`,
  `review_code.run` invokes `review_with_fallback` exactly ONCE per round
  with the state-default frontier provider, the prompt bytes match
  today's baseline, and no `code_review.first_pass.md` file is ever
  produced.
- **Case A — manual reviewer bypasses staging.** With
  `get_reviewer_adapter(state) is None` (`state["models"]["reviewer"] ==
  "human"`) AND staging configured, `review_code.run` takes the manual
  branch and never calls `review_with_fallback*`.
- **Case A — prior manual_required bypasses staging.** With
  `"review_code" in state["manual_review_required"]` AND staging configured
  AND a headless adapter otherwise resolvable, `review_code.run` takes the
  manual branch and never calls `review_with_fallback*`.
- **Case B / C — staging ON, frontier round uses `review_with_fallback`.**
  With `implement_round_count > escalate_after` and staging configured,
  the runner invokes `review_with_fallback` (the today function, NOT
  `_for_provider`) exactly once and does not create
  `code_review.first_pass.md`.
- **Case D — routing progression.** With staging enabled and
  `escalate_after == N`, monkeypatching so the runner observes which
  provider key was invoked: `implement_round_count` values `1..N` dispatch
  the first-pass provider through `review_with_fallback_for_provider`;
  `N+1` and beyond dispatch the frontier provider through
  `review_with_fallback`.
- **Case D — cheap-APPROVED promotion.** With staging enabled, the round
  dispatched to the first-pass reviewer, and the first
  `review_with_fallback_for_provider` returning `parse_status == "ok"` +
  `decision == "APPROVED"`: the runner invokes `review_with_fallback` a
  second time within the same `run()` call, `PhaseResult.staging_audit`
  is a non-empty string identifying the promotion round.
- **Case D — cheap-APPROVED promotion (frontier rejects).** Same setup but
  the second call returns `CHANGES_REQUESTED`; the final `PhaseResult` has
  `status == "changes_requested"`, `code_review.md` contains the
  frontier raw, and `code_review.first_pass.md` contains the first-pass raw.
- **Case D — first-pass CHANGES_REQUESTED is NOT promoted.** With staging
  enabled and the round dispatched to the first-pass reviewer, if
  `review_with_fallback_for_provider` returns `CHANGES_REQUESTED`, the
  wrapper is invoked exactly ONCE per `run()`, no second call is issued,
  and `PhaseResult.status == "changes_requested"`.
- **Case D — non-ok / MANUAL_REQUIRED / non-APPROVED verdicts are NOT
  promoted.** With staging enabled, first-pass verdicts
  `RESCUE_REQUIRED`, `ASK_USER`, `parse_status == "unparseable"`, and
  `parse_status == MANUAL_REQUIRED` each cause exactly ONE wrapper call
  per `run()` and produce the appropriate non-approved `PhaseResult` /
  manual-required block.
- **Approval-authority invariant (hard).** A future refactor that lets a
  first-pass APPROVED map directly to `PhaseResult(status="approved")`
  fails this regression test. Written to survive control-flow rewrites
  (asserts on the SECOND invocation identity, not on incidental
  intermediate state).
- **Wrapper shares the ladder with `review_with_fallback`.** A test drives
  `review_with_fallback_for_provider` through the same INFRA-failure
  scenarios covered by `test_reviewer_fallback.py` (primary INFRA failure,
  fallback = `worker_provider`, fallback not read-only-enforced, fallback
  unknown, manual fallback) and asserts identical `ReviewResult` shapes.
- **Unknown first-pass provider fails MANUAL_REQUIRED, not silent
  approval.** If `get_reviewer_adapter_by_provider(review_stages.first_pass_reviewer)`
  returns `None` at dispatch time (registry mismatch),
  `review_with_fallback_for_provider` returns a `MANUAL_REQUIRED`
  result with an audit body naming the mismatch. Mirrors the existing
  "no headless reviewer adapter configured" branch.
- **First-pass same-provider collapse fails closed (D8).** With
  `state.models.implementer == "claude-sonnet-4-6"` and
  `review_stages.first_pass_reviewer == "claude"` (frontier reviewer left
  cross-provider), `orchestrator._adversarial_pairing_error(state)`
  returns a non-`None` error naming `self-review` on the first-pass side.
  Mirror test for the codex-worker side.
- **Cross-provider staging passes the guard.** With cross-provider
  staging (worker=claude, first_pass=codex, frontier=codex), the guard
  returns `None`.
- **Guard does NOT fire when staging is OFF.** With `review_stages is
  None` and the existing frontier-side pairing intact, the D8 addition
  makes no observable difference.
- **`review_audit` receives the promotion.** After a promoted round, the
  orchestrator's `review_audit` list contains an entry with
  `phase == "review_code"` and a `reason` string sourced from
  `PhaseResult.staging_audit`, appended at the same site as the
  `fallback_audit` wiring (`orchestrator.py:1448-1450`).
- **First-pass artifact rotation.** After N promoted rounds, files
  `code_review.first_pass.round1.md` …
  `code_review.first_pass.round(N-1).md` and the latest
  `code_review.first_pass.md` all exist. Driven by the extended
  `_clear_manual_phase_artifacts` making an explicit second
  `_archive_review_round` call for `review_code`.
- **`plan_review` is not routed through staging.** A regression test
  asserts that `plan_review.run` still calls `review_with_fallback(state,
  ...)` (not `_for_provider`) so the runner's dispatch scope stays
  `review_code` only.
- **Dogfood-config assertion.** Loading this repo's own
  `.redteam/config.toml` yields `cfg.models.review_stages is None`.

Test scaffolding restrictions (mirroring sibling tests in `.redteam/tests/`):
monkeypatch `_REVIEWER_ADAPTERS`, `review_with_fallback`,
`review_with_fallback_for_provider`, `get_reviewer_adapter_by_provider`,
`compute_repo_diff`, `repo_root`, `git_rev_parse`, and any narrowed-diff
probes. Do NOT spawn `codex` / `claude` subprocesses and do NOT touch a
real remote.

## Risks
- **Interaction with the narrowed-prompt runner (#92 Proposal 2).** The
  narrowed prompt uses `state["last_reviewed_rev"]` and open
  `review_items`. A promoted round issues TWO ladder calls, both seeing
  the same `last_reviewed_rev`; the runner MUST NOT overwrite
  `last_reviewed_rev` between the two calls and MUST set it exactly once
  at end-of-round from the FRONTIER call's HEAD. The existing
  `test_review_code_narrow_context.py` suite catches a violation, and a
  new regression asserts single-set-per-round.
- **Extra token cost of promotion.** A cheap round that APPROVES incurs
  BOTH the cheap and the frontier call — worst case is no savings on
  approve rounds, savings only on reject rounds. This matches the input's
  cost model. P5 will add the ceiling that bounds worst case.
- **Legacy state (`implement_round_count` absent).** A resumed mid-flight
  task from before this change may lack the counter. Treating a missing
  counter as `0` still routes the first `review_code` after `implement`
  through the first-pass reviewer (round 1 by intent), because
  `implement` increments the counter before `review_code` runs. Edge
  case: if `review_code` re-runs without `implement` re-running the
  counter can be stale by one — acceptable, because the next iteration
  corrects it.
- **Adapter identifier source of truth.** `first_pass_reviewer` is
  validated against `adapters._REVIEWER_ADAPTERS.keys()`. `config.py`
  today imports lazily from `phase_runners._base`; the implementer must
  decide between (i) a lazy import of `_REVIEWER_ADAPTERS` inside
  `_validate` OR (ii) exposing `known_reviewer_providers()` from
  `adapters/__init__.py`. Either satisfies the `Done-when`; the import
  direction MUST NOT create a cycle (`adapters` already imports from
  `phase_runners._base`).
- **P5 stackability.** Task-002 (P5) will add hard ceilings. The nested
  `[models.review_stages]` subtable was chosen so P5 can add keys like
  `max_review_rounds` alongside without another `[models]` restructure.
  The implementer must not add ceiling logic here.
