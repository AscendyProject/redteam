"""#95 — shell-native interactive `config` subcommand for per-role models.

`update_models_block` edits existing canonical `[models]` assignments in place (or
returns None to fail closed); `cmd_config` prompts, enforces the cross-provider
self-review invariant across every effective config, and writes atomically behind a
two-layer corruption gate.
"""

from __future__ import annotations

import builtins

import pytest


def _orch():
    import _engine

    return _engine.orchestrator()


def _config():
    import sys
    from pathlib import Path

    wf = Path(__file__).resolve().parents[1] / "workflows"
    if str(wf) not in sys.path:
        sys.path.insert(0, str(wf))
    import config

    return config


_BASE_MODELS = (
    "[models]\n"
    "# role -> model (comment must survive)\n"
    'planner = "claude-opus-4-7"\n'
    'implementer = "claude-sonnet-4-6"  # inline comment must survive\n'
    'reviewer = "codex"\n'
    'rescue = "codex"\n'
)


def _write_repo(tmp_path, models_block=_BASE_MODELS, extra=""):
    rt = tmp_path / ".redteam"
    rt.mkdir()
    (rt / "config.toml").write_text(models_block + extra, encoding="utf-8")
    return tmp_path


# ---------- update_models_block (pure editor) ----------


def test_update_replaces_value_and_preserves_comments():
    config = _config()
    res = config.update_models_block(_BASE_MODELS, {"implementer": "claude-haiku-4-5"})
    assert res is not None
    cand, edited = res
    assert 'implementer = "claude-haiku-4-5"  # inline comment must survive' in cand
    assert "# role -> model (comment must survive)" in cand  # block comment intact
    assert config.load_config_from_text(cand).models.implementer == "claude-haiku-4-5"


def test_update_preserves_hash_inside_value_and_inline_comment():
    config = _config()
    block = '[models]\nplanner = "a#b" # note\nimplementer = "x"\nreviewer = "codex"\nrescue = "codex"\n'
    res = config.update_models_block(block, {"planner": "c#d"})
    assert res is not None
    cand, _ = res
    assert 'planner = "c#d" # note' in cand
    assert config.load_config_from_text(cand).models.planner == "c#d"


def test_update_preserves_crlf_byte_for_byte():
    config = _config()
    crlf = _BASE_MODELS.replace("\n", "\r\n")
    res = config.update_models_block(crlf, {"reviewer": "claude"})
    assert res is not None
    cand, _ = res
    assert "\r\n" in cand and "\n" not in cand.replace("\r\n", "")  # still CRLF only
    assert 'reviewer = "claude"\r\n' in cand


def test_update_only_touches_models_section():
    config = _config()
    block = _BASE_MODELS + '\n[project]\nplanner = "decoy"\n'  # same key name in another section
    res = config.update_models_block(block, {"planner": "claude-opus-4-8"})
    assert res is not None
    cand, _ = res
    assert 'planner = "decoy"' in cand  # the [project] line is untouched


def test_update_returns_none_for_unsupported_forms():
    config = _config()
    unsupported = [
        '[models]\nimplementer = "x"\nreviewer = "codex"\nrescue = "codex"\n',  # missing planner
        '[models]\nplanner = "a"\nplanner = "b"\nimplementer="x"\nreviewer="codex"\nrescue="codex"\n',  # dup
        '[models]\n"planner" = "x"\nimplementer="x"\nreviewer="codex"\nrescue="codex"\n',  # quoted key
        '[models]\nmodels.planner = "x"\n',  # dotted
        '[models]\nplanner = \'x\'\nimplementer="x"\nreviewer="codex"\nrescue="codex"\n',  # single-quoted
        '[models]\nplanner = "a\\"b"\nimplementer="x"\nreviewer="codex"\nrescue="codex"\n',  # escaped quote
        '[project]\nname = "x"\n',  # no [models] section
    ]
    for block in unsupported:
        assert config.update_models_block(block, {"planner": "y", "implementer": "z"}) is None, block


def test_update_refuses_unsafe_new_value():
    config = _config()
    assert config.update_models_block(_BASE_MODELS, {"planner": 'a"b'}) is None
    assert config.update_models_block(_BASE_MODELS, {"planner": "a\\b"}) is None


# ---------- cmd_config (interactive) ----------


def _feed(monkeypatch, answers):
    it = iter(answers)

    def fake_input(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)


def test_cmd_config_happy_path_writes(monkeypatch, tmp_path, capsys):
    orch = _orch()
    repo = _write_repo(tmp_path)
    # keep planner, change implementer, keep reviewer/rescue
    _feed(monkeypatch, ["", "claude-haiku-4-5", "", ""])
    assert orch.cmd_config(repo=repo) == 0
    config = _config()
    cfg = config.load_config(repo)
    assert cfg.models.implementer == "claude-haiku-4-5"
    assert cfg.models.planner == "claude-opus-4-7"  # untouched
    # comments survived the write
    assert "inline comment must survive" in (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")


def test_cmd_config_refuses_self_review(monkeypatch, tmp_path):
    orch = _orch()
    repo = _write_repo(tmp_path)
    before = (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")
    # implementer stays claude-* (worker=claude); set reviewer=claude → self-review
    _feed(monkeypatch, ["", "", "claude", ""])
    assert orch.cmd_config(repo=repo) == 2
    assert (repo / ".redteam" / "config.toml").read_text(encoding="utf-8") == before  # nothing written


def test_cmd_config_allows_rescue_collapse_not_a_headless_reviewer(monkeypatch, tmp_path):
    """#101 review: rescue is NOT a headless reviewer at runtime (the rescue runner
    validates a manual report and never resolves models.rescue to an adapter), so a
    rescue value resolving to the worker's own provider must NOT be refused — only the
    reviewer is enforced cross-provider. Pins the supported reverse pair
    implementer="codex" / reviewer="claude" with the default rescue="codex": the runtime
    runs it, so the CLI must write it. (Pre-fix this was wrongly refused with exit 2.)"""
    orch = _orch()
    repo = _write_repo(tmp_path)
    # implementer=codex (worker=codex); reviewer=claude (cross-provider); rescue kept
    # = "codex" → resolves to the codex worker but must be allowed (not a headless review).
    _feed(monkeypatch, ["", "codex", "claude", ""])
    assert orch.cmd_config(repo=repo) == 0
    cfg = _config().load_config(repo)
    assert cfg.models.implementer == "codex"
    assert cfg.models.reviewer == "claude"
    assert cfg.models.rescue == "codex"  # worker-provider rescue retained, not refused


def test_cmd_config_review_false_tier_allows_same_provider(monkeypatch, tmp_path):
    orch = _orch()
    # a review=false tier with a same-provider pair must NOT block (no headless review there)
    repo = _write_repo(
        tmp_path,
        extra='\n[tiers.0]\nreview = false\nmodels = { reviewer = "claude", implementer = "claude-sonnet-4-6" }\n'
        "[tier_triggers]\ndefault = 0\n",
    )
    _feed(monkeypatch, ["", "", "codex", "codex"])  # top-level stays cross-provider
    assert orch.cmd_config(repo=repo) == 0


def test_cmd_config_review_true_tier_override_collapse_refused(monkeypatch, tmp_path):
    orch = _orch()
    # a review=true tier overrides implementer to codex, inheriting top-level reviewer=codex → collapse
    repo = _write_repo(
        tmp_path,
        extra='\n[tiers.3]\nreview = true\nmodels = { implementer = "codex" }\n[tier_triggers]\ndefault = 3\n',
    )
    before = (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")
    _feed(monkeypatch, ["", "", "codex", "codex"])
    assert orch.cmd_config(repo=repo) == 2
    assert (repo / ".redteam" / "config.toml").read_text(encoding="utf-8") == before


def test_cmd_config_legacy_value_kept_on_blank(monkeypatch, tmp_path):
    orch = _orch()
    repo = _write_repo(
        tmp_path,
        models_block='[models]\nplanner = "claude-opus-4-7"\nimplementer = "claude-sonnet-4-6"\n'
        'reviewer = "gemini"\nrescue = "codex"\n',
    )
    _feed(monkeypatch, ["", "", "", ""])  # keep all, incl. legacy reviewer="gemini"
    assert orch.cmd_config(repo=repo) == 0
    assert _config().load_config(repo).models.reviewer == "gemini"  # retained, no break


def test_cmd_config_new_unknown_reviewer_warns_and_confirm_declines(monkeypatch, tmp_path):
    orch = _orch()
    repo = _write_repo(tmp_path)
    before = (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")
    # type a typo reviewer → confirm prompt → decline (n) → then EOF (no more answers) → abort
    _feed(monkeypatch, ["", "", "codx", "n"])
    assert orch.cmd_config(repo=repo) == 2
    assert (repo / ".redteam" / "config.toml").read_text(encoding="utf-8") == before


def test_cmd_config_eof_aborts(monkeypatch, tmp_path):
    orch = _orch()
    repo = _write_repo(tmp_path)
    before = (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")
    _feed(monkeypatch, [])  # immediate EOF
    assert orch.cmd_config(repo=repo) == 2
    assert (repo / ".redteam" / "config.toml").read_text(encoding="utf-8") == before


def test_cmd_config_missing_config_exits_2(monkeypatch, tmp_path):
    orch = _orch()
    _feed(monkeypatch, ["", "", "", ""])
    assert orch.cmd_config(repo=tmp_path) == 2  # no .redteam/config.toml


def test_cmd_config_gate_refuses_noncanonical_block(monkeypatch, tmp_path):
    orch = _orch()
    # valid TOML that loads, but the [models] block is non-canonical (quoted key) → gate None
    repo = _write_repo(
        tmp_path,
        models_block='[models]\n"planner" = "claude-opus-4-7"\nimplementer = "claude-sonnet-4-6"\n'
        'reviewer = "codex"\nrescue = "codex"\n',
    )
    before = (repo / ".redteam" / "config.toml").read_text(encoding="utf-8")
    _feed(monkeypatch, ["", "claude-haiku-4-5", "", ""])
    assert orch.cmd_config(repo=repo) == 2
    assert (repo / ".redteam" / "config.toml").read_text(encoding="utf-8") == before


# ---------- reviewer_family_provider regression ----------


def test_reviewer_family_provider_unchanged_behavior():
    import sys
    from pathlib import Path

    wf = Path(__file__).resolve().parents[1] / "workflows"
    if str(wf) not in sys.path:
        sys.path.insert(0, str(wf))
    import adapters

    assert adapters.reviewer_family_provider("codex") == "codex"
    assert adapters.reviewer_family_provider("claude") == "claude"
    assert adapters.reviewer_family_provider("gemini") is None  # unregistered → manual
    assert adapters.reviewer_family_provider("human") is None
    assert adapters.reviewer_family_provider(None) is None
    # reviewer_provider still resolves through it
    assert adapters.reviewer_provider({"models": {"reviewer": "codex"}}) == "codex"
    assert adapters.reviewer_provider({"models": {"reviewer": "gemini"}}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
