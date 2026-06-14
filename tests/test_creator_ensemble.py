"""Creator ensemble (2-3 agents in the implementer role). A weak free model
emits code that won't compile (non-ASCII '…', a truncation) and the error
only surfaces at integrate. The ensemble generates several candidates from
different free models and returns the first that passes a deterministic
compile gate, so the broken candidate never reaches the workspace. Size 1 is
the legacy single call."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb   # noqa: E402


def _reply(files):
    return json.dumps({"files": files}, ensure_ascii=False)


# ── deterministic compile gate ────────────────────────────────────────────

def test_clean_python_passes_gate():
    ok, n, why = rw._candidate_compiles(_reply({"src/a.py": "def f():\n    return 1\n"}))
    assert ok and n == 1 and why == ""


def test_non_ascii_fails_gate():
    ok, n, why = rw._candidate_compiles(_reply({"src/a.py": "x = …\n"}))
    assert not ok and "non-ASCII" in why


def test_syntax_error_fails_gate():
    ok, n, why = rw._candidate_compiles(_reply({"src/a.py": "def f(:\n    pass\n"}))
    assert not ok and "py" in why


def test_unparseable_reply_fails_gate():
    ok, n, why = rw._candidate_compiles("not json at all")
    assert not ok and n == 0


def test_non_python_files_ignored():
    ok, _, _ = rw._candidate_compiles(_reply({"README.md": "# …unicode ok here"}))
    assert ok


# ── ensemble selection ────────────────────────────────────────────────────

def test_ensemble_picks_first_clean_candidate(monkeypatch):
    lb.configure_workers({"implementer": {"models": ["free/a", "free/b", "free/c"]}})
    # first model emits a non-ASCII file, second emits clean code
    replies = iter([
        _reply({"src/x.py": "x = …\n"}),           # free/a — broken
        _reply({"src/x.py": "def x():\n    return 1\n"}),  # free/b — clean
    ])
    used = []

    def fake_round(prompt, *, role, node, system, allowed, disallowed, cwd,
                   model, channel, specialty="", **kw):
        used.append(model)
        return next(replies)

    monkeypatch.setattr(rw, "_dialog_round", fake_round)
    monkeypatch.setenv("SPEC_FLOW_CREATOR_ENSEMBLE", "3")
    out = rw._ensemble_generate("impl", node="x", system="s", allowed=[],
                                disallowed=[], cwd=".", model="free/a",
                                channel=None, specialty="")
    assert "def x()" in out          # the clean candidate won
    assert used == ["free/a", "free/b"]   # stopped early once clean
    lb.configure_workers(None)


def test_ensemble_size_one_is_single_call(monkeypatch):
    calls = []

    def fake_round(prompt, *, role, node, system, allowed, disallowed, cwd,
                   model, channel, specialty="", **kw):
        calls.append(model)
        return _reply({"src/x.py": "x = …\n"})   # broken, but N=1 takes it

    monkeypatch.setattr(rw, "_dialog_round", fake_round)
    monkeypatch.setenv("SPEC_FLOW_CREATOR_ENSEMBLE", "1")
    out = rw._ensemble_generate("impl", node="x", system="s", allowed=[],
                                disallowed=[], cwd=".", model="m",
                                channel=None, specialty="")
    assert len(calls) == 1 and "…" in out    # legacy: one call, no gating


def test_ensemble_all_broken_returns_best(monkeypatch):
    lb.configure_workers({"implementer": {"models": ["free/a", "free/b"]}})
    replies = iter([
        _reply({"src/x.py": "x = …\n"}),                 # 1 file, broken
        _reply({"src/x.py": "y = …\n", "src/z.py": "z = 1\n"}),  # 2 files, broken
    ])

    def fake_round(prompt, *, role, node, system, allowed, disallowed, cwd,
                   model, channel, specialty="", **kw):
        return next(replies)

    monkeypatch.setattr(rw, "_dialog_round", fake_round)
    monkeypatch.setenv("SPEC_FLOW_CREATOR_ENSEMBLE", "2")
    out = rw._ensemble_generate("impl", node="x", system="s", allowed=[],
                                disallowed=[], cwd=".", model="free/a",
                                channel=None, specialty="")
    # none clean → best by file count (the 2-file candidate)
    assert "src/z.py" in out
    lb.configure_workers(None)
