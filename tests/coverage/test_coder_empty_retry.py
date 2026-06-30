"""v130 root — a coder reply that writes ZERO files (a provider timeout degraded
the call to empty/prose) must not silently produce an empty leaf.

In v130 both the `core` and `product_entry` orchestra leaves hit
``openai backend timed out after 75s`` on the tester step; the coder wrote
nothing; the assembled product had no module and the boot-gate failed (honest
RED, but a wasted ~25-min run). ``_ensemble_generate`` now retries along the
fallback chain until at least one file lands — even for a leaf_small (ensemble
size 1) — capped at ``_MIN_NONEMPTY_ATTEMPTS``. Model-independent reliability:
a leaf is only RED on a real test failure, never on a provider hiccup.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "harness"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from harness import role_worker as rw  # noqa: E402
from harness import llm_backend as lb  # noqa: E402

_EMPTY = '{"files": {}}'
_WITH_FILES = '{"files": {"src/core.py": "x = 1\\n"}}'


def _patch(monkeypatch, replies, chain=("m1", "m2", "m3")):
    calls = []

    def fake_dialog(prompt, **kw):
        calls.append(kw.get("model"))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(rw, "_dialog_round", fake_dialog)
    monkeypatch.setattr(lb, "chain_for", lambda *a, **k: list(chain))
    monkeypatch.setattr(rw.llm_log, "log", lambda *a, **k: None)
    return calls


def _gen(complexity="leaf_small"):
    return rw._ensemble_generate(
        "build it", node="core", system="", allowed=[], disallowed=[],
        cwd="/tmp", model="lead", channel=None, specialty="",
        tier="", complexity=complexity, meta={}, params={})


def test_empty_first_reply_retries_until_files(monkeypatch):
    rw.llm_backend.WORKERS_CFG["creator_ensemble"] = 1
    calls = _patch(monkeypatch, [_EMPTY, _WITH_FILES])
    out = _gen("leaf_small")          # ensemble size 1 — still must retry
    assert "src/core.py" in out
    assert len(calls) == 2            # one retry happened
    assert calls[0] == "lead"        # first try keeps the resolved model
    assert calls[1] == "m2"          # retry walks the fallback chain


def test_first_reply_with_files_does_not_retry(monkeypatch):
    rw.llm_backend.WORKERS_CFG["creator_ensemble"] = 1
    calls = _patch(monkeypatch, [_WITH_FILES, _WITH_FILES])
    out = _gen("leaf_small")
    assert "src/core.py" in out
    assert len(calls) == 1            # legacy fast path: no wasted retry


def test_all_empty_stops_at_cap(monkeypatch):
    rw.llm_backend.WORKERS_CFG["creator_ensemble"] = 1
    calls = _patch(monkeypatch, [_EMPTY])
    out = _gen("leaf_small")
    assert out == _EMPTY                       # returns the best (empty) effort
    assert len(calls) == rw._MIN_NONEMPTY_ATTEMPTS    # bounded, not infinite


def test_single_model_chain_cannot_retry(monkeypatch):
    # nothing to fall back to → exactly one try, no wasted calls
    rw.llm_backend.WORKERS_CFG["creator_ensemble"] = 1
    calls = _patch(monkeypatch, [_EMPTY], chain=("only",))
    _gen("leaf_small")
    assert len(calls) == 1
