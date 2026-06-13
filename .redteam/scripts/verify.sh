#!/usr/bin/env bash
set -euo pipefail

# Project verify command (config.toml [project] verify_command points here).
# The implementer/test sub-agents run this and must report failures back to the
# orchestrator rather than papering over them.
#
# This default verifies THIS repo (the redteam harness itself: ruff + pytest
# over .redteam/). When you install the harness into your project, replace this
# with your stack's checks — `npm test`, `cargo test`, `go test ./...`,
# `ruff && mypy && pytest`, etc. See
# examples/fastapi-like/.redteam/scripts/verify.sh for a real example.

# Auto-activate a local venv if present, so the script works whether invoked
# from an activated shell or directly by the orchestrator. Handle both the POSIX
# layout (venv/bin/activate) and the Windows one (venv/Scripts/activate, used by
# Git Bash / MSYS where this script still runs under bash).
if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ -f "venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -f "venv/Scripts/activate" ]; then
        # shellcheck disable=SC1091
        source venv/Scripts/activate
    fi
fi

echo "=== ruff check ==="
ruff check .redteam/

echo "=== ruff format check ==="
ruff format --check .redteam/

echo "=== pytest ==="
pytest .redteam/tests -x --tb=short

echo "✅ verify.sh OK"
