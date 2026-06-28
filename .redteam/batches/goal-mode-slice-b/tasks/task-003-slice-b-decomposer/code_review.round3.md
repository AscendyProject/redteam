Disagree:

IR-006 severity:blocker status:open

`cmd_decompose` checks cross-provider pairing before invoking the untrusted decomposer worker, but then runs the decomposition review using live config after that worker has had workspace-write access. The guard runs at `.redteam/workflows/orchestrator.py:2232`, the mutating worker runs at `.redteam/workflows/orchestrator.py:2239`, and the review later calls `review_with_fallback({}, ...)` at `.redteam/workflows/orchestrator.py:2267`. With `{}` state, provider resolution falls back to live `.redteam/config.toml` via `default_model_for_role` (`.redteam/workflows/phase_runners/_base.py:30`, `.redteam/workflows/phase_runners/_base.py:40`) and `reviewer_provider` (`.redteam/workflows/adapters/__init__.py:85`, `.redteam/workflows/adapters/__init__.py:89`). `review_with_fallback` trusts the primary reviewer result without rechecking primary cross-provider pairing (`.redteam/workflows/adapters/__init__.py:152`, `.redteam/workflows/adapters/__init__.py:156`). A decomposer can therefore mutate `.redteam/config.toml` after the preflight guard, point the reviewer at the worker provider, and get same-provider decomposition review. This violates the project security checklist’s cross-provider fail-closed boundary. The review provider/model snapshot needs to be pinned before the worker runs, and the review call must use that snapshot or revalidate against an immutable pre-worker provider decision.

Uncertain:

I could not independently run `bash .redteam/scripts/verify.sh` in this read-only sandbox. The task state reports `verification.last_exit_code == 0`, and `verification.log` shows the project gate, focused decomposer suite, regression suites, and ruff passing.

Agree:

IR-001 severity:major status:resolved

The success-side decomposer runner contract is now enforced in `phase_runners/decompose.py`: success requires exit 0, a parseable `goal.json`, and non-empty briefs for every manifest task; missing, empty, or unparseable output returns `"error"` (`.redteam/workflows/phase_runners/decompose.py:107`, `.redteam/workflows/phase_runners/decompose.py:109`, `.redteam/workflows/phase_runners/decompose.py:115`, `.redteam/workflows/phase_runners/decompose.py:124`).

IR-004 severity:major status:resolved

The cannot-decompose stray-brief hole is fixed. The runner records `stray_briefs` and requires `not stray_briefs` before returning `"cannot_decompose"` (`.redteam/workflows/phase_runners/decompose.py:78`, `.redteam/workflows/phase_runners/decompose.py:93`).

IR-005 severity:major status:resolved

The cannot-decompose stray-state hole is fixed. The runner records `stray_states` and requires `not stray_states` before accepting the marker as valid cannot-decompose (`.redteam/workflows/phase_runners/decompose.py:79`, `.redteam/workflows/phase_runners/decompose.py:94`).

The new decomposer prompt/agent packaging is present, `decompose` is a separate subcommand, idempotency fails closed on existing `goal.json` or task `input.md`, non-APPROVED review persists `decompose_review.md`, and approved manifests are loaded through `_load_goal_manifest`. The new tests are discriminating against pre-change code because `cmd_decompose`, `phase_runners/decompose.py`, and the decomposer packaging did not exist on the baseline.

REVIEW_DECISION: CHANGES_REQUESTED
