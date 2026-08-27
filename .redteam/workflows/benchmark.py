"""Benchmark data-layer and execution layer for the redteam harness Phase 1 MVP.

Cross-cutting invariants:
- Zero non-stdlib imports: only tomllib, json, shutil, statistics, subprocess, sys,
  pathlib, tempfile, time, datetime, dataclasses, typing.
- No filesystem side effects at import time.
- Never opens .redteam/config.toml or any .redteam/batches/ path for WRITE in the
  operator's real repo; isolation is enforced by subprocess + tempcopy.

Exposes:
- BenchmarkSet          — frozen dataclass: parsed benchmark.toml + task enumeration.
- load_benchmark_set()  — fail-loud loader (mirrors config.py's discipline).
- BenchmarkRecord       — TypedDict: deterministic fields for one result record.
- append_record()       — JSONL append helper (creates parent dir/file on first call).
- load_records()        — JSONL reader; raises ValueError on malformed lines.
- completed_triples()   — (config, task, repetition) resume set from a record list.
- extract_metrics()     — deterministic metric extractor over state["phase_telemetry"].
- run_one()             — isolated subprocess dispatch for one (config, task, rep) triple.
- run_benchmark()       — outer loop: configs × tasks × repetitions, resumable + budgeted.
- build_report()        — pure: markdown diff table from config_names + records list.
- run_report()          — I/O wrapper: reads results.jsonl, prints report to stdout.
"""
# NOTE: benchmark.py source intentionally contains no "gh ", "git push", "pr create",
# "--force", or auto-merge language, and does not import phase_runners.create_pr.
# This is enforced by a static grep test in test_benchmark_runner.py.

from __future__ import annotations

import datetime
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL = frozenset({"repetitions", "budget_usd", "configs", "copy_exclude"})
_KNOWN_ROLES = frozenset({"planner", "implementer", "reviewer", "rescue"})

# Snapshot exclusions for run_one's copytree, split by whether a set may override them.
#
# Mandatory — never overridable, because dropping one breaks isolation or correctness:
#   .git          the tempcopy is `git init`'d fresh on 'main'; real history would hand
#                 the pre-implement floors a trust root from the operator's own repo.
#   batches       the run seeds its own batch; real batch state would be re-entered.
#   results{,.jsonl}  the run writes its own records.
_MANDATORY_COPY_EXCLUDE = (".git", "batches", "results", "results.jsonl")

# Default — size optimizations a set MAY override via `copy_exclude` in benchmark.toml.
# Excluding `venv`/`.venv` silently breaks a verify_command that depends on a
# project-local virtualenv: the tools are not on PATH in the tempcopy, so every
# verification exec fails with 127 and the task churns to `deferred` at full cost
# while its metrics describe a broken environment rather than a model combination
# (#185). These names are also Python-stack fingerprints, which belong in
# project-owned config rather than the engine.
DEFAULT_COPY_EXCLUDE = ("venv", ".venv", "__pycache__", "*.egg-info")


# ---------------------------------------------------------------------------
# BenchmarkSet — parsed result of benchmark.toml + tasks/ enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkSet:
    """Frozen snapshot of a parsed benchmark set.

    Attributes:
        repetitions: Number of times each (config, task) pair is run. >= 1.
        budget_usd:  Best-effort Claude-cost cap in USD (or None for no cap). It
                     stops dispatching further triples once the accumulated
                     observed cost this invocation (plus a prior-data estimate)
                     would reach the cap. Because a triple's cost is unknown
                     before it runs, a run with no prior cost estimate — notably
                     the FIRST paid run — can overshoot the cap; run `--dry-run`
                     once some records exist for a tighter pre-estimate. NOT a
                     hard ceiling on the first run.
        configs:     Ordered mapping of config-name → {role: model-id} overrides.
                     Declaration order from benchmark.toml is preserved.
        task_ids:    Sorted tuple of task ids that have a non-empty input.md.
        copy_exclude: Overridable half of run_one's snapshot exclusions, defaulting
                     to DEFAULT_COPY_EXCLUDE. _MANDATORY_COPY_EXCLUDE is always
                     applied on top, so a set cannot drop `.git` / `batches` /
                     `results`. A set whose verify_command needs a project-local
                     virtualenv omits `venv`/`.venv` here to have it copied (#185).
    """

    repetitions: int
    budget_usd: float | None
    configs: dict[str, dict[str, str]]
    task_ids: tuple[str, ...]
    copy_exclude: tuple[str, ...] = DEFAULT_COPY_EXCLUDE


def load_benchmark_set(set_root: Path) -> BenchmarkSet:
    """Load <set_root>/benchmark.toml and enumerate <set_root>/tasks/<id>/input.md.

    Raises ValueError (naming the offending key/section) on:
    - missing benchmark.toml
    - unknown top-level key (catches [[benchmark.matrix]] as key "benchmark")
    - [configs.<name>] sub-key not in {planner, implementer, reviewer, rescue}
    - repetitions: bool, non-int, or < 1
    - budget_usd: bool, non-number, or <= 0
    - zero [configs.*] tables
    - configs.<name>.<role> value: bool, non-string, empty, or whitespace-only string
    - copy_exclude: not a list, or an element that is bool, non-string, or blank
    - tasks tree with no non-empty input.md
    """
    toml_path = set_root / "benchmark.toml"
    if not toml_path.exists():
        raise ValueError(f"benchmark.toml not found at {toml_path}.")

    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    # Unknown top-level keys — also catches [[benchmark.matrix]] (becomes key "benchmark")
    unknown = set(data) - _KNOWN_TOP_LEVEL
    if unknown:
        raise ValueError(
            f"Unknown top-level key(s) in benchmark.toml: {sorted(unknown)}. Known keys: {sorted(_KNOWN_TOP_LEVEL)}."
        )

    # repetitions — default 1; bool rejected (bool is int subclass in Python)
    reps = data.get("repetitions", 1)
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        raise ValueError(f"repetitions must be an int >= 1 (bool values rejected), got {reps!r}.")

    # budget_usd — optional; None means no cap; bool rejected
    budget_raw = data.get("budget_usd")
    budget: float | None
    if budget_raw is None:
        budget = None
    else:
        if isinstance(budget_raw, bool) or not isinstance(budget_raw, (int, float)) or budget_raw <= 0:
            raise ValueError(f"budget_usd must be a number > 0 (bool values rejected), got {budget_raw!r}.")
        budget = float(budget_raw)

    # configs — at least one [configs.<name>] table required
    configs_raw = data.get("configs")
    if not isinstance(configs_raw, dict) or len(configs_raw) == 0:
        raise ValueError("benchmark.toml must contain at least one [configs.<name>] table.")

    configs: dict[str, dict[str, str]] = {}
    for cfg_name, table in configs_raw.items():
        if not isinstance(table, dict):
            raise ValueError(f"[configs.{cfg_name}] must be a table, got {table!r}.")
        unknown_roles = set(table) - _KNOWN_ROLES
        if unknown_roles:
            raise ValueError(
                f"[configs.{cfg_name}] has unknown key(s): {sorted(unknown_roles)}. "
                f"Known roles: {sorted(_KNOWN_ROLES)}."
            )
        role_overrides: dict[str, str] = {}
        for role, value in table.items():
            # bool is a subclass of int/str in Python — reject it explicitly first
            if isinstance(value, bool) or not isinstance(value, str):
                raise ValueError(
                    f"configs.{cfg_name}.{role} must be a non-empty string "
                    f"(bool and non-string values rejected), got {value!r}."
                )
            if not value.strip():
                raise ValueError(
                    f"configs.{cfg_name}.{role} must be a non-empty string (got empty or whitespace-only string)."
                )
            role_overrides[role] = value
        configs[cfg_name] = role_overrides

    # copy_exclude — optional; replaces DEFAULT_COPY_EXCLUDE. An explicit empty list is
    # honoured (it still leaves _MANDATORY_COPY_EXCLUDE in force), so `is None` rather
    # than falsiness decides whether the default applies.
    exclude_raw = data.get("copy_exclude")
    if exclude_raw is None:
        copy_exclude = DEFAULT_COPY_EXCLUDE
    else:
        if not isinstance(exclude_raw, list):
            raise ValueError(f"copy_exclude must be a list of strings, got {exclude_raw!r}.")
        for item in exclude_raw:
            if isinstance(item, bool) or not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"copy_exclude entries must be non-empty strings "
                    f"(bool, non-string and blank values rejected), got {item!r}."
                )
        copy_exclude = tuple(exclude_raw)

    # task_ids — enumerate non-empty input.md files; at least one required
    tasks_dir = set_root / "tasks"
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        for entry in tasks_dir.iterdir():
            if not entry.is_dir():
                continue
            input_md = entry / "input.md"
            if input_md.is_file() and input_md.stat().st_size > 0:
                task_ids.append(entry.name)

    if not task_ids:
        raise ValueError(
            f"No non-empty input.md found under {tasks_dir}; at least one task with a non-empty input.md is required."
        )

    return BenchmarkSet(
        repetitions=reps,
        budget_usd=budget,
        configs=configs,
        task_ids=tuple(sorted(task_ids)),
        copy_exclude=copy_exclude,
    )


# ---------------------------------------------------------------------------
# BenchmarkRecord — deterministic schema for one (config, task, repetition) run
# ---------------------------------------------------------------------------


class BenchmarkRecord(TypedDict):
    """One result record written to results.jsonl.

    schema_version = 2. Fields are deterministic; never fabricate values:
    claude_cost_usd is None when only Codex-role phases ran.

    v1 → v2 (#172): rescue_count became nullable and its meaning changed. A v1
    record stored 0 because the old extractor counted `rescue` telemetry entries
    that the engine never writes — a fabricated zero, not a measurement. The
    version bump is what lets a reader tell the two apart; without it a stored 0
    is ambiguous and aggregation silently treats it as measured.
    """

    schema_version: int  # 1 = pre-#172 (rescue_count fabricated as 0); 2 = current
    config: str  # [configs.<name>] key from benchmark.toml
    task: str  # task id (subdir name under tasks/)
    repetition: int  # 1-indexed
    outcome: str  # "done" | "deferred" | "error"
    review_rounds: int
    retry_count: int
    # None = not measured, never 0-by-default (#172). The rescue phase invokes no
    # model — it validates a manually produced rescue_report.md — so it emits no
    # telemetry to count, and rescue_entry_count is a budget counter reset on
    # convergence. Recording 0 would render as a measured "no rescues ever".
    rescue_count: int | None
    scope_creep_count: int  # floor-trip count
    wall_clock_sec: float
    claude_cost_usd: float | None  # None when only Codex-role phases ran
    started_at: str  # ISO-8601 UTC string
    finished_at: str  # ISO-8601 UTC string


# ---------------------------------------------------------------------------
# JSONL store — append/read/resume helpers
# ---------------------------------------------------------------------------


def append_record(jsonl_path: Path, record: dict) -> None:
    """Append one compact JSON object line to jsonl_path.

    Creates the parent directory and file on first call. Writes exactly one
    JSON object followed by a newline per call. Single f.write() call under
    open(..., "a") is sufficient for the MVP (no fsync/locking needed).
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(line)


def load_records(jsonl_path: Path) -> list[dict]:
    """Parse jsonl_path line by line, returning records in append order.

    Silently skips blank lines. Raises ValueError naming the file path and the
    1-indexed line number on any malformed JSON line (fail-loud on corruption;
    the JSONL file is the source of truth).
    """
    records: list[dict] = []
    text = jsonl_path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            raise ValueError(f"Malformed JSON at {jsonl_path}:{lineno}: {line!r}.")
    return records


def completed_triples(records: list[dict]) -> set[tuple[str, str, int]]:
    """Return (config, task, repetition) triples for every record present.

    Records with outcome="error" or outcome="deferred" still count as completed
    for resume purposes — the runner never silently re-runs to chase a better
    result; that is a Phase 2 concern.
    """
    return {(r["config"], r["task"], r["repetition"]) for r in records}


# ---------------------------------------------------------------------------
# Metric extractor — deterministic metrics from a completed task state.json
# ---------------------------------------------------------------------------

# Floor-trip feedback prefixes emitted by phase_runners/implement.py (#91/#117).
# These are the actually-emitted strings for:
#   - _floor_outside_scope ("refusing to sweep operator tracked WIP…")
#   - _cross_run_trust_root_floor ("cross-run trust-root floor: outside-scope paths…")
# Both end up in deferred_requirements[].feedback when retries are exhausted (reason="stalled").
# No floor-trip-specific reason string exists, so we match on the stable feedback prefix.
_FLOOR_TRIP_FEEDBACK_PREFIXES = (
    "cross-run trust-root floor:",
    "refusing to sweep operator tracked WIP",
)


def extract_metrics(state: dict) -> dict:
    """Extract deterministic benchmark metrics from a completed task state dict.

    Reads only fields the engine already emits; never adds new state.json keys.

    Returns a mapping with keys:
      outcome, review_rounds, retry_count, rescue_count, scope_creep_count,
      wall_clock_sec, claude_cost_usd.
    """
    next_phase = state.get("next_phase", "")
    if next_phase == "done":
        outcome = "done"
    elif state.get("deferred") is True or next_phase == "deferred":
        outcome = "deferred"
    else:
        outcome = "error"

    telemetry: list[dict] = state.get("phase_telemetry", [])
    review_rounds = sum(1 for e in telemetry if e.get("phase") == "review_code")
    # rescue_count: from the durable cumulative counter (#172), NOT from telemetry
    # (rescue.py invokes no model, so it writes no entry) and NOT from
    # rescue_entry_count (a budget, zeroed on convergence). Absent key → None:
    # the state predates the counter, so the value is unmeasured rather than zero.
    # A state seeded from the current template always carries it, so a task that
    # simply never rescued correctly reports a measured 0.
    _rescue_total = state.get("rescue_total_count")
    rescue_count = int(_rescue_total) if _rescue_total is not None else None
    # retry_count: deterministic sum of the per-phase retry counter (existing field, no new key).
    retry_count = sum(state.get("retries", {}).values())
    # scope_creep_count: floor-trip count from deferred_requirements.
    # The engine records floor-trip events (pre-worker out-of-scope tracked floor #91
    # and cross-run trust-root floor #117) as PhaseResult(status="error") feedback;
    # when retries are exhausted they land in deferred_requirements as reason="stalled"
    # with the original floor-trip message preserved in the "feedback" field.
    # Both stable feedback prefixes are from phase_runners/implement.py — not invented.
    deferred_requirements = state.get("deferred_requirements", [])
    scope_creep_count = sum(
        1 for e in deferred_requirements if e.get("feedback", "").startswith(_FLOOR_TRIP_FEEDBACK_PREFIXES)
    )
    wall_clock_sec = sum((e.get("duration_sec") or 0.0) for e in telemetry)
    # claude_cost_usd: sum costs from Claude-provider phases; None when no Claude entry.
    # Never fabricate a cost from Codex-only runs.
    claude_costs = [e["cost_usd"] for e in telemetry if e.get("provider") == "claude" and e.get("cost_usd") is not None]
    claude_cost_usd: float | None = sum(claude_costs) if claude_costs else None

    return {
        "outcome": outcome,
        "review_rounds": review_rounds,
        "retry_count": retry_count,
        "rescue_count": rescue_count,
        "scope_creep_count": scope_creep_count,
        "wall_clock_sec": wall_clock_sec,
        "claude_cost_usd": claude_cost_usd,
    }


# ---------------------------------------------------------------------------
# Internal helpers for run_one isolation
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Real repo root. benchmark.py lives at <repo>/.redteam/workflows/benchmark.py."""
    return Path(__file__).resolve().parents[2]


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _toml_value(v: object) -> str:
    """Format a Python scalar or list-of-scalars as a TOML value fragment."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return repr(v)


def _write_merged_config(dest_path: Path, base_config: dict, config_overrides: dict) -> None:
    """Write a merged config.toml to dest_path.

    Preserves all non-[models] sections from base_config; merges the [models]
    section with config_overrides (overrides win). Never writes to the real
    config.toml in the operator's repo.
    """
    merged_models = {**base_config.get("models", {}), **config_overrides}

    lines: list[str] = ["# benchmark-generated merged config\n"]
    for section, values in base_config.items():
        if section == "models":
            continue
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")

    lines.append("[models]")
    for role, model_id in merged_models.items():
        lines.append(f'{role} = "{model_id}"')
    lines.append("")

    dest_path.write_text("\n".join(lines), encoding="utf-8")


# Bootstrap driver template written to <tempcopy>/_bench_driver.py.
# Formatted with .format(batch_dir_repr=repr(str(batch_dir))).
#
# Safety contract (enforced by static test in test_benchmark_runner.py):
# - Contains no "gh ", "git push", "pr create", or "--force" literals.
# - Runtime-rebinds orchestrator.PHASE_RUNNERS["create_pr"] to a no-op BEFORE
#   cmd_start is called, so the real create_pr runner is never invoked.
# - Defense-in-depth: even if the rebind were bypassed, the tempcopy has no
#   'origin' remote, so create_pr.py's git-remote preflight (create_pr.py:82)
#   fails closed before any push/PR action.
_BENCH_DRIVER_TEMPLATE = """\
import sys
import pathlib

_BATCH_DIR = pathlib.Path({batch_dir_repr})

# Insert the tempcopy's workflows dir so `import orchestrator` resolves to the
# tempcopy's copy — repo_root() then points to the tempcopy, not the real repo.
sys.path.insert(0, str(pathlib.Path(__file__).parent / ".redteam" / "workflows"))
import orchestrator  # noqa: E402


def _noop_create_pr(task_dir, state):
    \"\"\"Neutralised create_pr: marks the task done without opening any PR.\"\"\"
    state["next_phase"] = "done"
    return {{"status": "approved", "feedback": "benchmark no-op", "log": "benchmark no-op", "diff": ""}}


# Runtime rebind: PHASE_RUNNERS is a module-level mutable dict (orchestrator.py:119),
# looked up per phase step (orchestrator.py:1364), so this takes effect before
# cmd_start dispatches any task.
orchestrator.PHASE_RUNNERS["create_pr"] = _noop_create_pr

sys.exit(orchestrator.cmd_start(_BATCH_DIR))
"""

# ---------------------------------------------------------------------------
# run_one — isolated subprocess dispatch for one (config, task, rep) triple
# ---------------------------------------------------------------------------


def run_one(
    set_root: Path,
    config_name: str,
    task_id: str,
    repetition: int,
    *,
    config_overrides: dict,
    workspace: Path,
) -> dict:
    """Real isolated dispatch for one benchmark triple.

    Isolation (subprocess + tempcopy, zero engine changes required):
    1. shutil.copytree snapshot of the real repo → temp dir under workspace.
    2. git init the snapshot on branch 'main'; no 'origin' remote configured.
    3. Write merged .redteam/config.toml into the snapshot (real config untouched).
    4. Seed the task's input.md into the batch dir under the snapshot.
    5. Write a bootstrap driver that rebinds PHASE_RUNNERS["create_pr"] to a no-op.
    6. subprocess.run the driver (cwd=tempcopy, sys.executable).
    7. Read state.json from the snapshot; call extract_metrics; build record.
    8. TemporaryDirectory context manager auto-deletes the snapshot on exit.

    Bound on that isolation (#185): the snapshot isolates the *repo*, not the
    *toolchain*. A set that opts its virtualenv into the copy (see copy_exclude)
    gets a venv whose bin/activate and console-script shebangs still hold the
    ORIGINAL absolute prefix, so verification executes the host's interpreter and
    tools, not the snapshot's. Measured, not assumed — see
    test_copied_virtualenv_still_resolves_to_the_original_prefix. Making the copy
    relocatable is unreliable (upstream removed `virtualenv --relocatable`), and
    provisioning a fresh environment inside the snapshot needs a stack-specific
    install command the engine must not encode. The toolchain is therefore shared
    across every run of a sweep — constant, so it cannot confound a between-config
    comparison, but it is not hermetic and a run that installs packages would reach
    the operator's environment.

    wall_clock_sec is measured via time.monotonic() around the subprocess call, NOT
    re-derived from telemetry sums (which miss non-worker phases like plan_review).
    started_at / finished_at are ISO-8601 UTC strings.
    """
    real_repo = _repo_root()
    real_config = tomllib.loads((real_repo / ".redteam" / "config.toml").read_text(encoding="utf-8"))
    # The set owns the overridable half of the snapshot exclusions, so it is read here
    # rather than threaded through run_benchmark's injectable run_one seam.
    copy_exclude = load_benchmark_set(set_root).copy_exclude

    started_at = _utc_now_iso()
    t_start = time.monotonic()

    with tempfile.TemporaryDirectory(dir=workspace) as td:
        tempcopy = Path(td) / "repo"

        # Step 1: snapshot harness tree. Mandatory exclusions are unioned in last so a
        # set's copy_exclude cannot drop .git / batches / results (#185).
        shutil.copytree(
            str(real_repo),
            str(tempcopy),
            ignore=shutil.ignore_patterns(*copy_exclude, *_MANDATORY_COPY_EXCLUDE),
        )

        # Step 2: git init on main, no origin remote
        for cmd in (
            ["git", "init"],
            ["git", "config", "user.email", "bench@redteam.local"],
            ["git", "config", "user.name", "benchmark"],
            ["git", "add", "-A"],
            ["git", "commit", "-m", "benchmark seed"],
        ):
            subprocess.run(cmd, cwd=str(tempcopy), check=True, capture_output=True, encoding="utf-8")
        # Rename to 'main' (handles git < 2.28 where default branch is 'master')
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=str(tempcopy),
            check=False,
            capture_output=True,
            encoding="utf-8",
        )

        # Step 3: write merged config.toml (real repo config is never opened for write)
        _write_merged_config(
            tempcopy / ".redteam" / "config.toml",
            real_config,
            config_overrides,
        )

        # Step 4: seed the task's input.md into the batch dir
        batch_name = f"bench-{config_name}-{task_id}-rep{repetition}"
        batch_dir = tempcopy / ".redteam" / "batches" / batch_name
        task_dir_in_batch = batch_dir / "tasks" / task_id
        task_dir_in_batch.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            str(set_root / "tasks" / task_id / "input.md"),
            str(task_dir_in_batch / "input.md"),
        )

        # Step 5: write bootstrap driver
        driver_path = tempcopy / "_bench_driver.py"
        driver_text = _BENCH_DRIVER_TEMPLATE.format(batch_dir_repr=repr(str(batch_dir)))
        driver_path.write_text(driver_text, encoding="utf-8")

        # Step 6: run driver subprocess (cwd=tempcopy so repo_root() resolves correctly)
        subprocess.run(
            [sys.executable, "-u", str(driver_path)],
            cwd=str(tempcopy),
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=1800,  # 30-minute hard cap per run
        )

        t_end = time.monotonic()
        finished_at = _utc_now_iso()
        wall_clock_sec = t_end - t_start

        # Step 7: read state.json and extract metrics
        state_path = task_dir_in_batch / "state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            metrics = extract_metrics(state)
        else:
            # Subprocess failed before seeding state — all metrics are error defaults
            metrics = {
                "outcome": "error",
                "review_rounds": 0,
                "retry_count": 0,
                "rescue_count": None,  # unmeasured, not "no rescues" (#172)
                "scope_creep_count": 0,
                "wall_clock_sec": 0.0,
                "claude_cost_usd": None,
            }

        return {
            "schema_version": 2,
            "config": config_name,
            "task": task_id,
            "repetition": repetition,
            "outcome": metrics["outcome"],
            "review_rounds": metrics["review_rounds"],
            "retry_count": metrics["retry_count"],
            "rescue_count": metrics["rescue_count"],
            "scope_creep_count": metrics["scope_creep_count"],
            "wall_clock_sec": wall_clock_sec,  # monotonic-measured; NOT telemetry sum
            "claude_cost_usd": metrics["claude_cost_usd"],
            "started_at": started_at,
            "finished_at": finished_at,
        }
        # TemporaryDirectory context manager auto-deletes tempcopy on exit


# ---------------------------------------------------------------------------
# run_benchmark — outer loop: configs × tasks × repetitions, resumable + budgeted
# ---------------------------------------------------------------------------


def run_benchmark(
    set_root: Path,
    *,
    dry_run: bool = False,
    run_one: "Callable[..., dict]" = run_one,
) -> int:
    """Configs × tasks × repetitions benchmark loop, resumable and budget-fenced.

    Budget scope (per-invocation): accumulated = sum of claude_cost_usd for records
    appended in THIS call only.  Prior JSONL records (already spent and skipped by
    resume) do NOT count toward the in-invocation budget.  Cross-invocation cumulative
    budgeting is a Phase 2 refinement.

    Returns:
      0   — normal completion (including after run_one errors that are caught + continued)
      3   — budget abort (a pending dispatch would exceed set.budget_usd)
    """
    bset = load_benchmark_set(set_root)
    results_path = set_root / "results.jsonl"

    # Load existing records for resume / cost estimation
    prior_records: list[dict] = []
    if results_path.is_file():
        prior_records = load_records(results_path)

    done = completed_triples(prior_records)

    # Build the full plan in declaration order: configs × task_ids × repetitions
    plan = [
        (cfg, tid, rep)
        for cfg in bset.configs
        for tid in bset.task_ids
        for rep in range(1, bset.repetitions + 1)
        if (cfg, tid, rep) not in done
    ]
    skipped = len(done)

    # Cost estimate from prior records (used by dry-run and budget check).
    prior_claude_costs = [r["claude_cost_usd"] for r in prior_records if r.get("claude_cost_usd") is not None]
    estimated_next: float | None = statistics.mean(prior_claude_costs) if prior_claude_costs else None

    if dry_run:
        planned = len(plan)
        if estimated_next is not None:
            cost_str = f"{estimated_next * planned:.4f}"
        else:
            cost_str = "unknown"
        print(f"benchmark dry-run: planned={planned} skipped={skipped} estimated_cost_usd={cost_str}")
        return 0

    # Live run: iterate the plan, budget-check before each dispatch.
    # this_inv_records tracks only records appended in THIS invocation (budget scope).
    # Cold-start heads-up: a budget cap with no prior cost estimate cannot bound the
    # first run(s) (a triple's cost is unknown before it runs), so it can overshoot
    # until cost data accumulates. Warn legibly — do NOT abort or change dispatch.
    if bset.budget_usd is not None and estimated_next is None:
        print(
            f"benchmark: warning — budget_usd=${bset.budget_usd:.4f} is set but there is no "
            "prior cost data to estimate against; the first run(s) may overshoot the cap "
            "until cost accumulates. Run `--dry-run` after some records exist for a tighter bound.",
            file=sys.stderr,
        )
    this_inv_records: list[dict] = []
    workspace = Path(tempfile.gettempdir())

    for cfg, tid, rep in plan:
        # Budget check BEFORE dispatch (fail-loud on cost)
        if bset.budget_usd is not None:
            accumulated = sum((r.get("claude_cost_usd") or 0.0) for r in this_inv_records)
            est = estimated_next if estimated_next is not None else 0.0
            if accumulated + est >= bset.budget_usd:
                print(
                    f"benchmark: aborting before {cfg}/{tid}/rep={rep}: "
                    f"accumulated ${accumulated:.4f} + estimate ${est:.4f} "
                    f"would exceed budget ${bset.budget_usd:.4f}",
                    file=sys.stderr,
                )
                return 3

        # Dispatch
        try:
            record = run_one(
                set_root,
                cfg,
                tid,
                rep,
                config_overrides=bset.configs[cfg],
                workspace=workspace,
            )
        except Exception:
            now = _utc_now_iso()
            record = {
                "schema_version": 2,
                "config": cfg,
                "task": tid,
                "repetition": rep,
                "outcome": "error",
                "review_rounds": 0,
                "retry_count": 0,
                "rescue_count": None,  # unmeasured, not "no rescues" (#172)
                "scope_creep_count": 0,
                "wall_clock_sec": 0.0,
                "claude_cost_usd": None,
                "started_at": now,
                "finished_at": now,
            }

        # Append immediately so Ctrl-C between runs still resumes cleanly
        append_record(results_path, record)
        this_inv_records.append(record)

    return 0


# ---------------------------------------------------------------------------
# build_report — pure aggregation + markdown diff table
# ---------------------------------------------------------------------------


def build_report(config_names: list[str], records: list[dict]) -> str:
    """Build a markdown diff table from declared config names and raw records.

    Pure: no I/O. config_names drives column ordering (declaration order from
    benchmark.toml), so a zero-record config is still shown as a column (PR-001).
    Records whose "config" key is not in config_names are silently ignored.

    Formatting rules:
    - Floats: f"{x:.2f}"
    - Rates: f"{x:.2f}%" (percentage form)
    - Costs: f"${x:.2f}" or "n/a" (never "$0.00" when there is no real data)
    - Integers: plain int str
    - n/a: any cell that cannot be computed (zero total, zero done, no cost data)
    """
    # Group records by config in insertion order; unknown configs skipped
    by_config: dict[str, list[dict]] = {name: [] for name in config_names}
    for r in records:
        cfg = r.get("config", "")
        if cfg in by_config:
            by_config[cfg].append(r)

    def _cells(name: str) -> dict[str, str]:
        recs = by_config[name]
        total = len(recs)
        done_count = sum(1 for r in recs if r.get("outcome") == "done")
        deferred_count = sum(1 for r in recs if r.get("outcome") == "deferred")
        error_count = sum(1 for r in recs if r.get("outcome") == "error")

        sample = f"{total} (done={done_count}, deferred={deferred_count}, error={error_count})"
        if total == 0:
            return {
                "sample_size": sample,
                "approval_rate": "n/a",
                "avg_review_rounds": "n/a",
                "retry_rate": "n/a",
                "rescue_rate": "n/a",
                "scope_creep_rate": "n/a",
                "avg_wall_clock_sec": "n/a",
                "claude_cost": "n/a",
            }

        approval = f"{done_count / total * 100:.2f}%"
        avg_rounds = f"{sum(r.get('review_rounds', 0) for r in recs) / total:.2f}"
        retry = f"{sum(r.get('retry_count', 0) for r in recs) / total * 100:.2f}%"
        # None-aware, and divided by the MEASURED values only. Dividing by `total`
        # would let unmeasured records dilute a real rate — a resumed set mixing
        # legacy counts with unmeasured ones would report [1, 0, None, None] as
        # 25.00% when the measured subset is 50.00%.
        # A v1 record's rescue_count is a fabricated 0, not a measurement (#172),
        # so it is excluded from the denominator as well as the numerator —
        # otherwise a resumed set of [0, 0, None, None] still reports 0.00%.
        rescue_measured = [
            v
            for v in (r.get("rescue_count") if int(r.get("schema_version") or 1) >= 2 else None for r in recs)
            if v is not None
        ]
        rescue = f"{sum(rescue_measured) / len(rescue_measured) * 100:.2f}%" if rescue_measured else "n/a"
        scope = f"{sum(r.get('scope_creep_count', 0) for r in recs) / total * 100:.2f}%"
        avg_wall = f"{sum(r.get('wall_clock_sec', 0.0) for r in recs) / total:.2f}"

        if done_count == 0:
            cost_cell = "n/a"
        else:
            done_costs = [
                r["claude_cost_usd"]
                for r in recs
                if r.get("outcome") == "done" and r.get("claude_cost_usd") is not None
            ]
            cost_cell = f"${sum(done_costs) / done_count:.2f}" if done_costs else "n/a"

        return {
            "sample_size": sample,
            "approval_rate": approval,
            "avg_review_rounds": avg_rounds,
            "retry_rate": retry,
            "rescue_rate": rescue,
            "scope_creep_rate": scope,
            "avg_wall_clock_sec": avg_wall,
            "claude_cost": cost_cell,
        }

    aggs = {name: _cells(name) for name in config_names}

    metric_rows = [
        ("sample_size", "Sample size"),
        ("approval_rate", "Approval rate"),
        ("avg_review_rounds", "Avg review rounds"),
        ("retry_rate", "Retry rate"),
        ("rescue_rate", "Rescue rate"),
        ("scope_creep_rate", "Scope-creep rate"),
        ("avg_wall_clock_sec", "Avg wall-clock sec"),
        ("claude_cost", "Claude cost / approved task"),
    ]

    header = "| Metric | " + " | ".join(config_names) + " |"
    sep = "| --- |" + " --- |" * len(config_names)
    lines = [header, sep]
    for key, label in metric_rows:
        cells = " | ".join(aggs[n][key] for n in config_names)
        lines.append(f"| {label} | {cells} |")

    # Notes — ordered list comprehension preserves config_names declaration order
    total_records = len(records)
    zero_configs = [name for name in config_names if not by_config[name]]

    notes: list[str] = ["", "## Notes", ""]
    notes.append(f"- {total_records} record(s) read.")
    if zero_configs:
        notes.append(f"- Zero-record config(s): {', '.join(zero_configs)}.")
    notes.append("- Codex-role phases contribute n/a to the Claude cost column; costs are never fabricated.")

    return "\n".join(lines) + "\n" + "\n".join(notes) + "\n"


# ---------------------------------------------------------------------------
# run_report — I/O wrapper around build_report
# ---------------------------------------------------------------------------


def run_report(set_root: Path) -> int:
    """Read results.jsonl, build a markdown diff report, and print to stdout.

    Returns 0 on success. Returns 2 when the JSONL is missing or has zero
    records, naming `orchestrator benchmark <set>` as the next step.
    """
    results_path = set_root / "results.jsonl"
    if not results_path.exists():
        print(
            f"no benchmark results yet — run `orchestrator benchmark {set_root}` first",
            file=sys.stderr,
        )
        return 2

    records = load_records(results_path)
    if not records:
        print(
            f"no benchmark results yet — run `orchestrator benchmark {set_root}` first",
            file=sys.stderr,
        )
        return 2

    bset = load_benchmark_set(set_root)
    config_names = list(bset.configs.keys())
    sys.stdout.write(build_report(config_names, records))
    return 0
