#!/usr/bin/env bash
set -euo pipefail

# Project verify command — `.redteam/config.toml [project] verify_command` points
# here. The implementer / test sub-agents run this as THE gate and must report its
# failures back to the orchestrator rather than papering over them.
#
# THIS IS A TEMPLATE. Replace the gate at the bottom with YOUR stack's
# lint/type/test checks, e.g.:
#   ruff check . && ruff format --check . && pytest -x --tb=short
#   npm test
#   cargo test
#   go test ./...
# See examples/fastapi-like/.redteam/scripts/verify.sh in the redteam repo for a
# real, fleshed-out example.

# Auto-activate a local venv if present, so the script works whether invoked from
# an activated shell or directly by the orchestrator. Handles the POSIX layout
# (venv/bin/activate) and the Windows one (venv/Scripts/activate, under Git Bash /
# MSYS where this still runs as bash).
if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ -f "venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -f "venv/Scripts/activate" ]; then
        # shellcheck disable=SC1091
        source venv/Scripts/activate
    fi
fi

# --- REPLACE EVERYTHING BELOW with your project's gate. ---
# Until you do, this fails CLOSED: an unconfigured gate must never be read as a
# passing one (that would let the harness ship unverified changes).
echo "verify.sh is not configured yet." >&2
echo "Edit .redteam/scripts/verify.sh to run your project's lint/type/test gate" >&2
echo "(see the comments at the top of this file)." >&2
exit 1
