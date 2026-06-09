#!/usr/bin/env python3
"""redteam harness installer (vendoring model).

Copies the harness into a target project. The harness ships its engine *inside*
the project tree (`.redteam/workflows/...`) rather than as a site-packages
install, because the engine resolves the repo root from its own file location
(`.redteam/workflows/phase_runners/_base.py` → parents[3]). Vendoring keeps that
invariant true in any consumer repo.

Two file classes:

* **harness-owned** — the engine and its generic assets. Re-vendored on every
  install (overwritten only with --overwrite, so a plain re-run is safe by
  default and won't clobber local engine edits unless asked):
    .redteam/workflows/   .redteam/prompts/   .redteam/templates/
    .redteam/scripts/install.py
    .claude/agents/*.md   (generic sub-agent skeletons)

* **project-owned** — what you fill in for your stack. Seeded ONCE if absent,
  never overwritten (even with --overwrite), so your edits and task state are
  safe:
    .redteam/config.toml        (seeded from .redteam/templates/config.toml)
    .redteam/docs/*.md          (template skeletons you flesh out)
    .redteam/scripts/verify.sh  (your lint/type/test gate)
    .redteam/batches/           (your tasks + run state — created empty)

Usage:
    python3 .redteam/scripts/install.py <target-dir> [--overwrite] [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Source root = the harness repo this script ships in
# (.redteam/scripts/install.py → parents[2]).
SOURCE_ROOT = Path(__file__).resolve().parents[2]

# Directory subtrees re-vendored on install (harness-owned). These live entirely
# under .redteam/ which the harness owns, so replacing the whole subtree on
# --overwrite is safe.
HARNESS_TREES = (
    ".redteam/workflows",
    ".redteam/prompts",
    ".redteam/templates",
)

# Individual harness-owned files copied by name. The agent skeletons are copied
# file-by-file (NOT as a .claude/agents tree) because a consumer repo may keep
# its own unrelated Claude agents in that directory — re-vendoring must never
# delete them.
HARNESS_AGENTS = (
    "code-security-reviewer",
    "implementer",
    "outcome-planner",
    "pr-author",
    "test-author",
    "test-verifier",
)
HARNESS_FILES = (
    ".redteam/scripts/install.py",
    *(f".claude/agents/{name}.md" for name in HARNESS_AGENTS),
)

# Project-owned files seeded once. (dest relpath, source relpath-or-None).
# None source → create an empty file/dir. config.toml seeds from the template.
PROJECT_SEEDS = (
    (".redteam/config.toml", ".redteam/templates/config.toml"),
    (".redteam/docs/project-context.md", ".redteam/docs/project-context.md"),
    (".redteam/docs/security-checklist.md", ".redteam/docs/security-checklist.md"),
    (".redteam/docs/test-conventions.md", ".redteam/docs/test-conventions.md"),
    (".redteam/scripts/verify.sh", ".redteam/scripts/verify.sh"),
)

# Directories created empty if absent (project-owned run state).
PROJECT_DIRS = (".redteam/batches",)


def _log(action: str, path: str, dry: bool) -> None:
    prefix = "DRY " if dry else ""
    print(f"{prefix}{action:8} {path}")


def _copy_tree(rel: str, target: Path, overwrite: bool, dry: bool) -> None:
    src = SOURCE_ROOT / rel
    dst = target / rel
    if not src.is_dir():
        print(f"WARN     source tree missing, skipped: {rel}", file=sys.stderr)
        return
    if dst.exists() and not overwrite:
        _log("keep", rel + "/  (exists; --overwrite to refresh)", dry)
        return
    _log("vendor", rel + "/", dry)
    if dry:
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".mypy_cache", "*.pyc"))


def _copy_file(rel: str, target: Path, overwrite: bool, dry: bool) -> None:
    src = SOURCE_ROOT / rel
    dst = target / rel
    if not src.is_file():
        print(f"WARN     source file missing, skipped: {rel}", file=sys.stderr)
        return
    if dst.exists() and not overwrite:
        _log("keep", rel + "  (exists; --overwrite to refresh)", dry)
        return
    _log("vendor", rel, dry)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _seed_file(dst_rel: str, src_rel: str, target: Path, dry: bool) -> None:
    src = SOURCE_ROOT / src_rel
    dst = target / dst_rel
    if dst.exists():
        _log("keep", dst_rel + "  (project-owned; left as-is)", dry)
        return
    if not src.is_file():
        print(f"WARN     seed source missing, skipped: {src_rel}", file=sys.stderr)
        return
    _log("seed", dst_rel, dry)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _seed_dir(rel: str, target: Path, dry: bool) -> None:
    dst = target / rel
    if dst.exists():
        return
    _log("seed", rel + "/", dry)
    if dry:
        return
    dst.mkdir(parents=True, exist_ok=True)
    (dst / ".gitkeep").touch()


def install(target: Path, overwrite: bool, dry: bool) -> None:
    target = target.resolve()
    if target == SOURCE_ROOT:
        sys.exit("ERROR: refusing to install the harness onto itself.")
    if not target.is_dir():
        sys.exit(f"ERROR: target is not a directory: {target}")
    print(f"Installing redteam harness into {target}")
    print(f"  source: {SOURCE_ROOT}")
    print(
        f"  mode:   {'overwrite' if overwrite else 'safe (keep existing harness files)'}{' [dry-run]' if dry else ''}"
    )
    print()
    for rel in HARNESS_TREES:
        _copy_tree(rel, target, overwrite, dry)
    for rel in HARNESS_FILES:
        _copy_file(rel, target, overwrite, dry)
    for dst_rel, src_rel in PROJECT_SEEDS:
        _seed_file(dst_rel, src_rel, target, dry)
    for rel in PROJECT_DIRS:
        _seed_dir(rel, target, dry)
    print()
    print("Done. Next steps:")
    print("  1) Edit .redteam/config.toml for your stack.")
    print("  2) Fill .redteam/docs/*.md (context, security checklist, test conventions).")
    print("  3) Point .redteam/scripts/verify.sh at your lint/type/test gate.")
    print("  4) Add a task batch under .redteam/batches/ and run the orchestrator.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Install the redteam harness into a project (vendoring).")
    ap.add_argument("target", help="Path to the target project root.")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Refresh harness-owned files even if they exist (project-owned files are never overwritten).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without writing.")
    args = ap.parse_args()
    install(Path(args.target), overwrite=args.overwrite, dry=args.dry_run)


if __name__ == "__main__":
    main()
