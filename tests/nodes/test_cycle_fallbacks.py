"""#40: cyclic primary rotation spreads load across a role's model chain,
and an ENDLESS-ROTATION GUARD stops it spinning forever on non-quota errors
or rejects — waiting only ever heals a quota exhaustion."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb  # noqa: E402


def _reset():
    lb.configure_workers(None)
    import harness.llm_backend as m
    m._cycle_n = 0


# ─── cyclic start rotation ────────────────────────────────────────────

def test_cycled_is_noop_without_flag():
    _reset()
    chain = ["a", "b", "c"]
    assert lb._cycled(chain, {}) == chain
    assert lb._cycled(chain, {"cycle_models": False}) == chain


def test_cycled_rotates_start_round_robin():
    _reset()
    chain = ["a", "b", "c"]
    cfg = {"cycle_models": True}
    # successive calls start at a, b, c, a … — the SET is unchanged each time
    assert lb._cycled(chain, cfg) == ["a", "b", "c"]
    assert lb._cycled(chain, cfg) == ["b", "c", "a"]
    assert lb._cycled(chain, cfg) == ["c", "a", "b"]
    assert lb._cycled(chain, cfg) == ["a", "b", "c"]


def test_cycled_single_entry_is_stable():
    _reset()
    assert lb._cycled(["only"], {"cycle_models": True}) == ["only"]


def test_cycle_counter_is_thread_safe():
    _reset()
    import threading
    seen = []
    lock = threading.Lock()

    def grab():
        n = lb._next_cycle()
        with lock:
            seen.append(n)

    ts = [threading.Thread(target=grab) for _ in range(50)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # every call got a distinct counter value — no lost increments
    assert sorted(seen) == list(range(50))


# ─── endless-rotation guard ───────────────────────────────────────────

def test_non_quota_error_does_not_spin_all_rounds(monkeypatch):
    _reset()
    # a model that ALWAYS raises a non-quota RuntimeError. Without the guard
    # the call would wait+retry quota_retries times (here 50). The guard must
    # bail after max_error_rounds full passes instead.
    lb.configure_workers({"quota_wait_s": 0, "quota_retries": 50,
                          "max_error_rounds": 3, "fallback_models": []})
    calls = {"n": 0}

    def boom(prompt, model, system, fallback=False, timeout=None):
        calls["n"] += 1
        raise RuntimeError("malformed response")

    monkeypatch.setattr(lb, "_ask_one", boom)
    monkeypatch.setattr(lb, "_concurrency_gate", lambda: None)
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)
    try:
        lb.ask("p", model="m", fallbacks=[])
        assert False, "must surrender, not loop forever"
    except RuntimeError as e:
        assert "malformed" in str(e)
    # one attempt + max_error_rounds(3) non-quota passes = 4 chain walks
    assert calls["n"] <= 4


def test_quota_error_still_waits_and_retries(monkeypatch):
    _reset()
    # a quota error DOES heal by waiting — the guard must not cut it short;
    # the model succeeds on the 3rd attempt.
    lb.configure_workers({"quota_wait_s": 0, "quota_retries": 5,
                          "max_error_rounds": 2, "fallback_models": []})
    calls = {"n": 0}

    def flaky(prompt, model, system, fallback=False, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise lb.QuotaExhausted("429")
        return "ok"

    monkeypatch.setattr(lb, "_ask_one", flaky)
    monkeypatch.setattr(lb, "_concurrency_gate", lambda: None)
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)
    assert lb.ask("p", model="m", fallbacks=[]) == "ok"
    assert calls["n"] == 3


def test_reset_after_module():
    _reset()
