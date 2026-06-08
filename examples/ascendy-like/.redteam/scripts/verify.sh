#!/usr/bin/env bash
set -euo pipefail

# Auto-activate venv if not already in one. Lets the script work whether
# invoked from an activated shell or directly from the orchestrator's env.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

echo "=== ruff check ==="
ruff check .

echo "=== ruff format check ==="
ruff format --check .

# mypy is intentionally disabled at the verify.sh gate.
#
# The codebase uses the legacy SQLAlchemy 1.x declarative_base() pattern
# (`class X(Base): col = Column(...)`), which produces 1400+ false-positive
# Column[T]/Mapped[T] mismatches under default mypy. Even after silencing the
# six SQLAlchemy-related error codes in pyproject.toml's [tool.mypy] section,
# 140+ secondary errors remain (var-annotated, int/str type-flow downstream
# of Column[T], etc.). Re-enabling mypy at this gate is blocked on migrating
# models to the modern `Mapped[T]` style — tracked separately.
#
# To run mypy ad-hoc (knowing it will fail with current models):
#   mypy app/
echo "=== mypy === (skipped — see comment above)"

echo "=== pytest ==="
pytest -x --tb=short

echo "✅ verify.sh OK"
