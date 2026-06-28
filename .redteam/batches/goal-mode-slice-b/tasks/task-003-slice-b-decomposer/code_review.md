Disagree:
No open findings.

Uncertain:
I did not run `bash .redteam/scripts/verify.sh` because this review is read-only/no file touching. I verified `verification.log` exists, `state.verification.last_exit_code == 0` is recorded at `.redteam/batches/goal-mode-slice-b/tasks/task-003-slice-b-decomposer/state.json:105`, and the log reports the project gate, focused decomposer tests, regression tests, and ruff checks passing.

Agree:
IR-007 severity:blocker status:resolved
The success-path stray `state.json` hole is fixed. `phase_runners/decompose.py` now scans `tasks/*/state.json` and returns `"error"` before success if any exists (`.redteam/workflows/phase_runners/decompose.py:78`, `.redteam/workflows/phase_runners/decompose.py:124`). `cmd_decompose` then aborts before review on runner `"error"` (`.redteam/workflows/orchestrator.py:2266`). Coverage exists for both direct runner behavior and `cmd_decompose` behavior (`.redteam/tests/test_goal_decomposer.py:478`, `.redteam/tests/test_goal_decomposer.py:511`).

IR-006 severity:blocker status:resolved
The decomposition review now uses a pre-worker provider/model snapshot. `cmd_decompose` reads reviewer/implementer defaults before invoking the worker, builds `_pinned_state`, runs `_adversarial_pairing_error`, rechecks after the worker against the same snapshot, and passes that pinned state to `review_with_fallback` (`.redteam/workflows/orchestrator.py:2237`, `.redteam/workflows/orchestrator.py:2245`, `.redteam/workflows/orchestrator.py:2247`, `.redteam/workflows/orchestrator.py:2285`, `.redteam/workflows/orchestrator.py:2296`). The mutation-regression test asserts the review receives the pre-worker model values (`.redteam/tests/test_goal_decomposer.py:239`, `.redteam/tests/test_goal_decomposer.py:278`).

IR-001 severity:major status:resolved
The runner contract is enforced in the runner, not just caller prose. Success requires exit 0, present/parseable `goal.json`, non-empty briefs for every manifest task, and no stray state (`.redteam/workflows/phase_runners/decompose.py:102`, `.redteam/workflows/phase_runners/decompose.py:107`, `.redteam/workflows/phase_runners/decompose.py:115`, `.redteam/workflows/phase_runners/decompose.py:128`). Missing/empty/unparseable cases are covered (`.redteam/tests/test_goal_decomposer.py:427`, `.redteam/tests/test_goal_decomposer.py:440`, `.redteam/tests/test_goal_decomposer.py:453`).

IR-004 severity:major status:resolved
Cannot-decompose no longer accepts stray briefs. The marker path requires no `tasks/*/input.md` before returning `"cannot_decompose"` (`.redteam/workflows/phase_runners/decompose.py:78`, `.redteam/workflows/phase_runners/decompose.py:88`), with regression coverage at `.redteam/tests/test_goal_decomposer.py:347`.

IR-005 severity:major status:resolved
Cannot-decompose no longer accepts stray task state. The marker path also requires no `tasks/*/state.json` (`.redteam/workflows/phase_runners/decompose.py:79`, `.redteam/workflows/phase_runners/decompose.py:94`), with regression coverage at `.redteam/tests/test_goal_decomposer.py:381`.

IR-002 severity:major status:resolved
The required Codex decomposer prompt is now a decomposer prompt, not a review prompt: it instructs reading `goal.md`, writing `goal.json` and briefs, honoring single-parent rules, using the cannot-decompose marker, and not seeding `state.json` (`.redteam/prompts/codex/goal_decomposer.md:3`, `.redteam/prompts/codex/goal_decomposer.md:6`, `.redteam/prompts/codex/goal_decomposer.md:63`, `.redteam/prompts/codex/goal_decomposer.md:80`).

IR-003 severity:minor status:resolved
The unrelated parent-slice task-state churn is no longer in the reviewed diff. The current diff is limited to the decomposer prompt/agent, packaging/install wiring, orchestrator/runner changes, and tests.

Other checks:
The `decompose` command remains separate from `start`/`resume` and stops before dispatch (`.redteam/workflows/orchestrator.py:2183`, `.redteam/workflows/orchestrator.py:2351`, `.redteam/workflows/orchestrator.py:2410`). Idempotency fails closed on existing `goal.json` or task `input.md` (`.redteam/workflows/orchestrator.py:2211`, `.redteam/workflows/orchestrator.py:2220`). Approved output is validated through existing `_load_goal_manifest`, preserving the Slice A/C path (`.redteam/workflows/orchestrator.py:2331`, `.redteam/workflows/orchestrator.py:790`). Packaging includes the new agent in installer and marketplace contracts (`.redteam/scripts/install.py:133`, `.claude-plugin/marketplace.json:19`, `.redteam/tests/test_plugin_packaging.py:29`).

New-test discrimination:
Most new tests would fail against the pre-change baseline because `cmd_decompose`, `phase_runners/decompose.py`, and the new decomposer packaging did not exist. The flat-mode/no-goal tests are regression guards required by outcome item (f), not primary discriminators; they are acceptable because the suite also contains multiple fail-closed tests that exercise new behavior directly.

Output validity:
No scoring/ranking/threshold output was introduced. The meaningful discriminator is the hard gate among `success`, `cannot_decompose`, and `error` in the runner, plus reviewer `APPROVED` vs non-approved in `cmd_decompose`; realistic partial-write, invalid-manifest, rejected-review, and valid-decomposition inputs route to distinct outcomes rather than collapsing to a constant.

REVIEW_DECISION: APPROVED
