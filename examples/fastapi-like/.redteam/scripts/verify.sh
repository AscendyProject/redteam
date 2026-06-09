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

# mypy is intentionally disabled at the verify.sh gate in this example.
#
# A codebase still on the legacy SQLAlchemy 1.x declarative_base() pattern
# (`class X(Base): col = Column(...)`) produces many false-positive
# Column[T]/Mapped[T] mismatches under default mypy. Re-enabling mypy at this
# gate is blocked on migrating models to the modern `Mapped[T]` style —
# tracked separately. (Your project may keep mypy enabled here instead.)
#
# To run mypy ad-hoc:
#   mypy app/
echo "=== mypy === (skipped — see comment above)"

echo "=== pytest ==="
pytest -x --tb=short

echo "✅ verify.sh OK"
