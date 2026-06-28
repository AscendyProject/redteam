Disagree:

IR-007 severity:blocker status:open

A successful decomposer run can still pre-seed `tasks/<id>/state.json`, and `cmd_decompose` can return APPROVED with that untrusted task state left in place. The approved outcome requires the decomposition gate to complete before “any task state is seeded” and says the per-task state-machine surface stays untouched until APPROVED (`outcome.md:7`, `outcome.md:25`, `outcome.md:91`). The runner rejects stray `state.json` only for cannot-decompose (`.redteam/workflows/phase_runners/decompose.py:78`, `.redteam/workflows/phase_runners/decompose.py:88`), but the success path checks only parseable `goal.json` and non-empty briefs (`.redteam/workflows/phase_runners/decompose.py:107`, `.redteam/workflows/phase_runners/decompose.py:115`, `.redteam/workflows/phase_runners/decompose.py:124`). `cmd_decompose` then proceeds through review, validation, and returns 0 without scanning for pre-existing task state (`.redteam/workflows/orchestrator.py:2271`, `.redteam/workflows/orchestrator.py:2296`, `.redteam/workflows/orchestrator.py:2331`, `.redteam/workflows/orchestrator.py:2351`). On the later `start`, `_run_one_task` trusts an existing `state.json` and skips template seeding (`.redteam/workflows/orchestrator.py:1634`, `.redteam/workflows/orchestrator.py:1637`, `.redteam/workflows/orchestrator.py:1639`). That lets the untrusted decomposer write the downstream state machine input before the decomposition is approved. Success should fail closed if any `tasks/*/state.json` exists, and the tests need a success-path stray-state case.

Uncertain:

I did not independently run `bash .redteam/scripts/verify.sh` because this review was requested as stdout-only/no file touching and the sandbox is read-only. I verified `verification.log` exists and `state.verification.last_exit_code == 0`; the log reports `443 passed` plus focused decomposer and ruff checks passing.

Agree:

IR-006 severity:blocker status:resolved

The cross-provider pinning issue is fixed for the primary decomposition review. `cmd_decompose` snapshots reviewer and implementer model defaults before the worker runs (`.redteam/workflows/orchestrator.py:2237`, `.redteam/workflows/orchestrator.py:2243`), runs `_adversarial_pairing_error` against that pinned state (`.redteam/workflows/orchestrator.py:2247`), rechecks after worker execution (`.redteam/workflows/orchestrator.py:2285`), and passes the pinned state into `review_with_fallback` (`.redteam/workflows/orchestrator.py:2296`).

IR-001 severity:major status:resolved

The success-side missing/empty brief contract is enforced in the runner: unparseable manifests, missing briefs, and empty briefs now return `"error"` before review (`.redteam/workflows/phase_runners/decompose.py:107`, `.redteam/workflows/phase_runners/decompose.py:109`, `.redteam/workflows/phase_runners/decompose.py:115`).

IR-004 severity:major status:resolved

Cannot-decompose no longer accepts stray task briefs: `stray_briefs` is collected and must be empty before returning `"cannot_decompose"` (`.redteam/workflows/phase_runners/decompose.py:78`, `.redteam/workflows/phase_runners/decompose.py:93`).

IR-005 severity:major status:resolved

Cannot-decompose no longer accepts stray task state: `stray_states` is collected and must be empty before returning `"cannot_decompose"` (`.redteam/workflows/phase_runners/decompose.py:79`, `.redteam/workflows/phase_runners/decompose.py:94`).

The new tests are mostly discriminating against the pre-change code because `cmd_decompose`, `phase_runners/decompose.py`, and the decomposer packaging did not exist on the baseline. The missing coverage is specifically the success-path stray `state.json` case above.

REVIEW_DECISION: CHANGES_REQUESTED
