"""Process tiering at the worker boundary: _select_team picks the implementer
path. An explicit per-leaf executor team wins; else a solo-flagged simple leaf
routes to the single-coder path ([]); else the configured orchestra. Pure +
offline — no LLM, no network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402


_TEAM = [{"role": "coder", "provider": "local"}]


def test_solo_suppresses_global_orchestra(monkeypatch):
    # a configured global team would normally run, but a solo leaf bypasses it
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    assert rw._select_team({"node": "n", "solo": True}) == []


def test_no_solo_runs_global_orchestra(monkeypatch):
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    assert rw._select_team({"node": "n"}) == _TEAM


def test_explicit_executor_team_beats_solo(monkeypatch):
    # an explicit per-leaf team (C1 domain routing) is a deliberate decision and
    # overrides the solo heuristic
    monkeypatch.setattr(rw, "_implementer_team", lambda: [])
    ctx = {"node": "n", "solo": True, "team": _TEAM}
    out = rw._select_team(ctx)
    assert len(out) == 1 and out[0]["role"] == "coder" \
        and out[0]["provider"] == "local"


def test_solo_emits_process_tier_log(monkeypatch):
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    seen = []
    monkeypatch.setattr(rw.llm_log, "log", lambda rec: seen.append(rec))
    rw._select_team({"node": "leaf7", "solo": True})
    tier = [r for r in seen if r.get("event") == "process_tier"]
    assert tier and tier[0]["mode"] == "solo" and tier[0]["node"] == "leaf7"


def test_no_team_no_solo_empty(monkeypatch):
    # no global team, no solo -> empty (single-agent default path) without a log
    monkeypatch.setattr(rw, "_implementer_team", lambda: [])
    seen = []
    monkeypatch.setattr(rw.llm_log, "log", lambda rec: seen.append(rec))
    assert rw._select_team({"node": "n"}) == []
    assert not [r for r in seen if r.get("event") == "process_tier"]


# --- _leaf_landed_ok: did the solo pass deliver a compiling module? ----------

def test_landed_ok_valid_module(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def f():\n    return 1\n")
    assert rw._leaf_landed_ok(str(tmp_path), "foo") is True


def test_landed_ok_missing_file(tmp_path):
    assert rw._leaf_landed_ok(str(tmp_path), "foo") is False


def test_landed_ok_empty_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("   \n")
    assert rw._leaf_landed_ok(str(tmp_path), "foo") is False


def test_landed_ok_syntax_error(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def (:\n")
    assert rw._leaf_landed_ok(str(tmp_path), "foo") is False


# --- creator ensemble scaled by node complexity ------------------------------

def test_ensemble_leaf_small_is_one(monkeypatch):
    # a trivial single-concern leaf generates ONE candidate even when 2 configured
    monkeypatch.setattr(rw.llm_backend, "WORKERS_CFG", {"creator_ensemble": 2})
    assert rw._ensemble_size("leaf_small") == 1


def test_ensemble_leaf_big_keeps_configured(monkeypatch):
    monkeypatch.setattr(rw.llm_backend, "WORKERS_CFG", {"creator_ensemble": 2})
    assert rw._ensemble_size("leaf_big") == 2


def test_ensemble_branch_keeps_configured(monkeypatch):
    monkeypatch.setattr(rw.llm_backend, "WORKERS_CFG", {"creator_ensemble": 3})
    assert rw._ensemble_size("branch") == 3


def test_ensemble_unknown_keeps_configured(monkeypatch):
    monkeypatch.setattr(rw.llm_backend, "WORKERS_CFG", {"creator_ensemble": 2})
    assert rw._ensemble_size("") == 2


def test_ensemble_clamped_to_four(monkeypatch):
    monkeypatch.setattr(rw.llm_backend, "WORKERS_CFG", {"creator_ensemble": 9})
    assert rw._ensemble_size("branch") == 4


