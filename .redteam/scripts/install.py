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
    .redteam/docs/*.md          (seeded from .redteam/templates/docs/*.md — generic
                                 skeletons you flesh out; this repo's own .redteam/docs/*
                                 are redteam-specific and are NOT what gets vendored)
    .redteam/scripts/verify.sh  (your lint/type/test gate)
    .redteam/batches/           (your tasks + run state — created empty)

Usage:
    python3 .redteam/scripts/install.py <target-dir> [--overwrite] [--dry-run] [--protect-config]
    python3 .redteam/scripts/install.py [<target-dir>] --check   # report version, no writes

--protect-config (opt-in, off by default) additionally merges Edit/Write deny
rules for .redteam/config.toml into the consumer's .claude/settings.json
(add-only, never clobbers). The orchestrator's runtime pairing guard is the
backstop regardless of whether this front-line friction is enabled.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# Source root = the harness repo this script ships in
# (.redteam/scripts/install.py → parents[2]).
SOURCE_ROOT = Path(__file__).resolve().parents[2]

# Harness version stamp written into a consumer on install (issue #34). Lets a
# consumer (and `--check`) tell which harness version is vendored and whether it's
# behind the source. Harness-owned: refreshed on every install, incl. --overwrite.
VERSION_STAMP_REL = ".redteam/.redteam-version"
REPO_URL = "https://github.com/AscendyProject/redteam"
# pyproject [project].name in the harness's OWN repo. Used to tell the source
# repo's pyproject apart from a consumer's: a vendored install.py has
# SOURCE_ROOT == the consumer repo, which may carry its own (unrelated) pyproject.
HARNESS_DIST_NAME = "redteam-harness"


def _pyproject_version(root: Path) -> str | None:
    """Version from root/pyproject.toml, but ONLY if it is the harness's own
    pyproject (project.name == redteam-harness). A consumer's pyproject is a
    different project whose version is NOT the harness version, so it must be
    ignored — otherwise a vendored install.py (SOURCE_ROOT = the consumer repo)
    would read the consumer's app version as the harness version (#34 PR-001)."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project", {})
    if not isinstance(project, dict) or project.get("name") != HARNESS_DIST_NAME:
        return None
    v = project.get("version")
    return v if isinstance(v, str) else None


def _stamp_version(root: Path) -> str | None:
    try:
        data = json.loads((root / VERSION_STAMP_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) else None


def _source_version() -> str | None:
    """The version the installer would vendor: the repo's pyproject when run from
    source, or this vendored copy's own stamp when install.py is itself vendored
    (a consumer has no pyproject.toml)."""
    return _pyproject_version(SOURCE_ROOT) or _stamp_version(SOURCE_ROOT)


def _git_short_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, ValueError):
        return None
    return out.stdout.strip() or None


def _parse_semver(v: str | None) -> tuple[int, ...] | None:
    if not isinstance(v, str):
        return None
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return None


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
    # Docs seed from the generic templates, NOT from this repo's own (filled-in,
    # redteam-specific) .redteam/docs/* — otherwise a consumer install would
    # inherit redteam's rules instead of a blank skeleton (project-agnosticism).
    (".redteam/docs/project-context.md", ".redteam/templates/docs/project-context.md"),
    (".redteam/docs/security-checklist.md", ".redteam/templates/docs/security-checklist.md"),
    (".redteam/docs/test-conventions.md", ".redteam/templates/docs/test-conventions.md"),
    # verify.sh seeds from the GENERIC template (fail-closed until configured), NOT
    # from this repo's own redteam-specific gate (ruff + pytest over .redteam/) —
    # a consumer must define their own stack's gate, not inherit redteam's (#43).
    (".redteam/scripts/verify.sh", ".redteam/templates/verify.sh"),
)

# Directories created empty if absent (project-owned run state).
PROJECT_DIRS = (".redteam/batches",)

# Consumer-owned Claude Code settings to merge a protection rule into.
SETTINGS_REL = ".claude/settings.json"
# Permission rules that stop a Claude Code agent from silently rewriting the
# harness's policy/model config. config.toml is the harness's "constitution": it
# decides which model writes vs. which (different) model reviews, so an agent
# editing it could collapse the adversarial pair into self-review. This is the
# FRONT-LINE prevention; the orchestrator's runtime pairing guard is the backstop
# that always catches a same-provider config regardless of how it got there.
# Caveat: Claude Code deny rules govern the Edit/Write tools only — they raise
# friction + signal intent, they are not airtight (e.g. a Bash `sed` could still
# reach the file), which is exactly why the runtime guard exists.
CONFIG_DENY_RULES = (
    "Edit(./.redteam/config.toml)",
    "Write(./.redteam/config.toml)",
)


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


# Seeded into the consumer's batches dir so the operator progress mirror (#49) is
# never committed into a PR. pr-author stages the whole task dir (`git add
# <task_dir>`), and progress.md is an operational surface, not part of the audit
# trail. The source repo handles this via its own root .gitignore; consumers get
# it from here (project-owned, seeded once, never overwritten).
BATCHES_GITIGNORE_REL = ".redteam/batches/.gitignore"
BATCHES_GITIGNORE_RULE = "**/progress.md"
BATCHES_GITIGNORE_BLOCK = (
    "# redteam run artifacts not meant for a PR (#49). The operator progress\n"
    "# mirror is an operational surface, not part of the audit trail, and\n"
    "# pr-author stages the whole task dir.\n"
    f"{BATCHES_GITIGNORE_RULE}\n"
)


def _seed_batches_gitignore(target: Path, dry: bool) -> None:
    """Ensure `.redteam/batches/.gitignore` ignores progress.md (#49), ADD-ONLY.

    pr-author stages the whole task dir, so without this a consumer would commit
    the operational progress mirror into the PR. Absent → create. Present but
    missing the rule (e.g. an existing install, or the consumer's own .gitignore)
    → APPEND the rule, preserving their content (never clobber project-owned
    files). Already present → no-op (idempotent). Mirrors the settings.json
    deny-merge discipline."""
    dst = target / BATCHES_GITIGNORE_REL
    if dst.exists():
        try:
            existing = dst.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError from a non-UTF-8 consumer file —
            # skip with guidance rather than tracebacking, and never clobber it
            # (mirrors the settings.json deny-merge's fail-safe).
            print(f"WARN     {BATCHES_GITIGNORE_REL} unreadable — skipped progress.md ignore ({exc}).", file=sys.stderr)
            return
        if any(line.strip() == BATCHES_GITIGNORE_RULE for line in existing.splitlines()):
            _log("keep", BATCHES_GITIGNORE_REL + "  (progress.md already ignored)", dry)
            return
        _log("merge", BATCHES_GITIGNORE_REL + "  (+progress.md ignore)", dry)
        if dry:
            return
        sep = "" if existing == "" or existing.endswith("\n") else "\n"
        dst.write_text(existing + sep + BATCHES_GITIGNORE_BLOCK, encoding="utf-8")
        return
    _log("seed", BATCHES_GITIGNORE_REL, dry)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(BATCHES_GITIGNORE_BLOCK, encoding="utf-8")


def _merge_settings_deny(target: Path, dry: bool) -> None:
    """Merge the config.toml protection rules into the consumer's
    `.claude/settings.json`, ADD-ONLY.

    `.claude/settings.json` is consumer-owned, so this never removes, reorders,
    or overwrites existing keys — it only appends any of CONFIG_DENY_RULES that
    are absent from `permissions.deny`, mirroring the never-clobber discipline
    the rest of the installer follows for project-owned files. Idempotent: a
    re-run with the rules already present is a no-op. Anything unexpected
    (unreadable file, non-object JSON, wrong types for `permissions`/`deny`) is
    skipped with a warning rather than risking corruption of a consumer file.
    """
    dst = target / SETTINGS_REL
    data: dict = {}
    if dst.exists():
        try:
            data = json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"WARN     {SETTINGS_REL} unreadable/invalid JSON — skipped deny-merge ({exc}).", file=sys.stderr)
            return
        if not isinstance(data, dict):
            print(f"WARN     {SETTINGS_REL} is not a JSON object — skipped deny-merge.", file=sys.stderr)
            return
    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        print(f"WARN     {SETTINGS_REL} 'permissions' is not an object — skipped deny-merge.", file=sys.stderr)
        return
    deny = perms.get("deny", [])
    if not isinstance(deny, list):
        print(f"WARN     {SETTINGS_REL} 'permissions.deny' is not a list — skipped deny-merge.", file=sys.stderr)
        return
    missing = [rule for rule in CONFIG_DENY_RULES if rule not in deny]
    if not missing:
        _log("keep", SETTINGS_REL + "  (config.toml deny rules already present)", dry)
        return
    _log("merge", SETTINGS_REL + f"  (+{len(missing)} config.toml deny rule(s))", dry)
    if dry:
        return
    deny.extend(missing)
    perms["deny"] = deny
    data["permissions"] = perms
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_version_stamp(target: Path, dry: bool) -> None:
    """Stamp the vendored harness version into the consumer (harness-owned).
    Refreshed on every install so `--check` can detect a stale vendored tree."""
    version = _source_version() or "unknown"
    _log("stamp", VERSION_STAMP_REL + f"  (harness {version})", dry)
    if dry:
        return
    stamp = {"version": version, "installed_from": _git_short_commit(SOURCE_ROOT), "source": str(SOURCE_ROOT)}
    dst = target / VERSION_STAMP_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")


def cmd_check(target: Path) -> int:
    """Read-only: report the vendored harness version vs the available (source)
    version and exit. 0 = up-to-date / ahead, 1 = outdated, 2 = cannot determine.
    No writes — safe to run from the vendored copy itself."""
    target = target.resolve()
    vendored = _stamp_version(target)
    available = _source_version()
    print(f"redteam harness version check ({target})")
    print(f"  vendored:  {vendored or 'unknown (unstamped — pre-#34 install)'}")
    print(f"  available: {available or 'unknown'}")
    sv, av = _parse_semver(vendored), _parse_semver(available)
    if sv is None or av is None:
        print("  verdict:   unknown — cannot compare versions.")
        return 2
    if sv < av:
        print(f"  verdict:   OUTDATED — re-vendor: python3 .redteam/scripts/install.py {target} --overwrite")
        print(f"             changes:  {REPO_URL}/compare/v{vendored}...v{available}")
        return 1
    if sv > av:
        print("  verdict:   ahead — the vendored copy is newer than the source.")
        return 0
    print("  verdict:   up-to-date.")
    return 0


def install(target: Path, overwrite: bool, dry: bool, protect_config: bool = False) -> None:
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
    _seed_batches_gitignore(target, dry)
    if protect_config:
        _merge_settings_deny(target, dry)
    _write_version_stamp(target, dry)
    print()
    print("Done. Next steps:")
    print("  1) Edit .redteam/config.toml for your stack.")
    print("  2) Fill .redteam/docs/*.md (context, security checklist, test conventions).")
    print("  3) Point .redteam/scripts/verify.sh at your lint/type/test gate.")
    print("  4) Add a task batch under .redteam/batches/ and run the orchestrator.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Install the redteam harness into a project (vendoring).")
    ap.add_argument("target", nargs="?", help="Path to the target project root.")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Refresh harness-owned files even if they exist (project-owned files are never overwritten).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without writing.")
    ap.add_argument(
        "--protect-config",
        action="store_true",
        help=(
            "Opt-in: merge Edit/Write deny rules for .redteam/config.toml into the consumer's "
            ".claude/settings.json (add-only, never clobbers). Off by default — the orchestrator's "
            "runtime pairing guard is the backstop regardless."
        ),
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only: report the vendored vs available harness version and exit "
            "(0 up-to-date, 1 outdated, 2 cannot determine) without vendoring anything. "
            "Target defaults to this script's own tree."
        ),
    )
    args = ap.parse_args()
    if args.check:
        raise SystemExit(cmd_check(Path(args.target) if args.target else SOURCE_ROOT))
    if not args.target:
        ap.error("target is required (or use --check)")
    install(Path(args.target), overwrite=args.overwrite, dry=args.dry_run, protect_config=args.protect_config)


if __name__ == "__main__":
    main()
