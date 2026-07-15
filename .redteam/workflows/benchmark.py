"""Benchmark data-layer for the redteam harness Phase 1 MVP.

Cross-cutting invariants:
- Zero non-stdlib imports: only tomllib, json, dataclasses, pathlib, datetime, typing.
- No filesystem side effects at import time.
- Never touches .redteam/config.toml or any .redteam/batches/ path.

Exposes:
- BenchmarkSet          — frozen dataclass: parsed benchmark.toml + task enumeration.
- load_benchmark_set()  — fail-loud loader (mirrors config.py's discipline).
- BenchmarkRecord       — TypedDict: deterministic fields for one result record.
- append_record()       — JSONL append helper (creates parent dir/file on first call).
- load_records()        — JSONL reader; raises ValueError on malformed lines.
- completed_triples()   — (config, task, repetition) resume set from a record list.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL = frozenset({"repetitions", "budget_usd", "configs"})
_KNOWN_ROLES = frozenset({"planner", "implementer", "reviewer", "rescue"})


# ---------------------------------------------------------------------------
# BenchmarkSet — parsed result of benchmark.toml + tasks/ enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkSet:
    """Frozen snapshot of a parsed benchmark set.

    Attributes:
        repetitions: Number of times each (config, task) pair is run. >= 1.
        budget_usd:  Hard cost ceiling in USD, or None for no cap.
        configs:     Ordered mapping of config-name → {role: model-id} overrides.
                     Declaration order from benchmark.toml is preserved.
        task_ids:    Sorted tuple of task ids that have a non-empty input.md.
    """

    repetitions: int
    budget_usd: float | None
    configs: dict[str, dict[str, str]]
    task_ids: tuple[str, ...]


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
    )


# ---------------------------------------------------------------------------
# BenchmarkRecord — deterministic schema for one (config, task, repetition) run
# ---------------------------------------------------------------------------


class BenchmarkRecord(TypedDict):
    """One result record written to results.jsonl.

    schema_version = 1 for this schema. Fields are deterministic; never fabricate
    values: claude_cost_usd is None when only Codex-role phases ran.
    """

    schema_version: int  # always 1
    config: str  # [configs.<name>] key from benchmark.toml
    task: str  # task id (subdir name under tasks/)
    repetition: int  # 1-indexed
    outcome: str  # "done" | "deferred" | "error"
    review_rounds: int
    retry_count: int
    rescue_count: int
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
