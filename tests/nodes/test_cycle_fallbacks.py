"""#40: cyclic primary rotation spreads load across a role's model chain,
and an ENDLESS-ROTATION GUARD stops it spinning forever on non-quota errors
or rejects — waiting only ever heals a quota exhaustion."""
import pathlib
import sys

import pytest  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb  # noqa: E402
from harness_fakeapi import ok          # noqa: E402


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

def test_non_quota_error_does_not_spin_all_rounds(fake_openai):
    _reset()
    # a model whose REAL endpoint always 500s -> _ask_openai raises a non-quota
    # RuntimeError. Without the guard the call would wait+retry quota_retries
    # times (here 50). The guard must bail after max_error_rounds passes. With
    # retries=1, one server hit == one chain walk, so the request count is the
    # number of full passes.
    srv = fake_openai([(500, "boom")], retries=1)
    lb.configure_workers({"quota_wait_s": 0, "quota_retries": 50,
                          "max_error_rounds": 3, "fallback_models": [],
                          "fallback_cooldown_s": 0})
    with pytest.raises(RuntimeError, match="HTTP 500"):
        lb.ask("p", model="x:free", fallbacks=[], role="decomposer", step="")
    # one attempt + max_error_rounds(3) non-quota passes = 4 chain walks
    assert srv.call_count <= 4


def test_quota_error_still_waits_and_retries(fake_openai):
    _reset()
    # a quota error (429) DOES heal by waiting — the guard must not cut it
    # short; the real endpoint 429s twice, then answers on the 3rd attempt.
    srv = fake_openai([(429, "rate"), (429, "rate"), (200, ok("ok"))], retries=1)
    lb.configure_workers({"quota_wait_s": 0, "quota_retries": 5,
                          "max_error_rounds": 2, "fallback_models": [],
                          "fallback_cooldown_s": 0})
    assert lb.ask("p", model="x:free", fallbacks=[], role="decomposer",
                  step="") == "ok"
    assert srv.call_count == 3


def test_timeout_classified_for_fast_retire():
    """Fast-fail mimo: the per-model breaker retires a model after ONE 'timeout'
    (a hung call we already paid for) but waits the threshold for a discrete
    '5xx'. This depends on _failure_reason classifying a client timeout as
    'timeout' and an HTTP 504 as '5xx' — capping the per-call timeout (p6
    workers.timeout=75) turns mimo's slow gateway 504 into a client timeout that
    is retired immediately (live v091: 25×~120s)."""
    assert lb._failure_reason(TimeoutError("the read operation timed out")) \
        == "timeout"
    assert lb._failure_reason(RuntimeError("openai backend failed: HTTP 504")) \
        == "5xx"
    assert lb._failure_reason(RuntimeError("HTTP 429 rate limited")) == "429"


def test_auth_failure_classified_and_retired_after_one():
    """Fast-fail #83: a 401/403 is broken provider auth that will not heal
    mid-run, so it is its own 'auth' reason and the breaker retires the model
    after ONE (live v094: xiaomimimo 401'd 16x, each a wasted instant fallback).
    A generic 4xx stays a legible 'http NNN' and is NOT auto-retired."""
    assert lb._failure_reason(RuntimeError("openai backend failed: HTTP 401")) \
        == "auth"
    assert lb._failure_reason(RuntimeError("HTTP 403 forbidden")) == "auth"
    assert lb._failure_reason(RuntimeError("HTTP 404 no such agent")) \
        == "http 404"


def test_reset_after_module():
    _reset()
