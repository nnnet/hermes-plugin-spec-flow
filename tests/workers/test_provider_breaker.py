"""The injected provider (the Bifrost chain) is tried FIRST on every ask(); a
dead upstream (repeated 504s) otherwise taxes every call and a single leaf can
burn 20+ min. A circuit breaker cuts the dead route: after N consecutive
provider failures it OPENS for a cooldown and calls skip straight to the working
local chain; one success closes it. Model-independent — keyed on provider:model,
so only the failing route is cut, healthy models keep flowing."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import llm_backend as lb   # noqa: E402

_CFG = {"provider_breaker_fails": 2, "provider_breaker_cooldown_s": 999}


def test_breaker_opens_after_threshold(monkeypatch):
    lb._provider_breaker.clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(lb.time, "monotonic", lambda: t["now"])
    assert not lb._breaker_open("bifrost:mimo")          # fresh route is closed
    lb._breaker_record("bifrost:mimo", False, _CFG)
    assert not lb._breaker_open("bifrost:mimo")          # one failure: still closed
    lb._breaker_record("bifrost:mimo", False, _CFG)
    assert lb._breaker_open("bifrost:mimo")              # second failure: OPEN


def test_breaker_closes_after_cooldown(monkeypatch):
    lb._provider_breaker.clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(lb.time, "monotonic", lambda: t["now"])
    lb._breaker_record("bifrost:mimo", False, _CFG)
    lb._breaker_record("bifrost:mimo", False, _CFG)
    assert lb._breaker_open("bifrost:mimo")
    t["now"] += 1000.0                                   # cooldown elapsed
    assert not lb._breaker_open("bifrost:mimo")


def test_success_resets_the_breaker(monkeypatch):
    lb._provider_breaker.clear()
    monkeypatch.setattr(lb.time, "monotonic", lambda: 0.0)
    lb._breaker_record("bifrost:mimo", False, _CFG)
    lb._breaker_record("bifrost:mimo", True, _CFG)        # a success clears it
    assert "bifrost:mimo" not in lb._provider_breaker


def test_only_the_dead_route_is_cut(monkeypatch):
    lb._provider_breaker.clear()
    monkeypatch.setattr(lb.time, "monotonic", lambda: 0.0)
    lb._breaker_record("bifrost:mimo", False, _CFG)
    lb._breaker_record("bifrost:mimo", False, _CFG)
    assert lb._breaker_open("bifrost:mimo")
    assert not lb._breaker_open("bifrost:other")          # a healthy route is untouched


def test_ask_skips_provider_once_open(monkeypatch):
    # once the breaker is open the dead provider is NOT called again — the call
    # goes straight to the chain (here empty, so ask raises, which we ignore)
    lb._provider_breaker.clear()
    monkeypatch.setattr(lb, "WORKERS_CFG", _CFG)
    monkeypatch.setattr(lb.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(lb, "chain_for", lambda role, specialty="": [])
    # fault-injection seam: every model attempt fails fast (offline), so ask()
    # exhausts and raises quickly without touching the network — we only count
    # how often the dead PROVIDER is reached
    monkeypatch.setattr(lb, "_ask_one",
                        lambda *a, **k: (_ for _ in ()).throw(lb.QuotaExhausted("down")))
    calls = {"n": 0}

    def dead(_prompt):
        calls["n"] += 1
        raise RuntimeError("http 504")

    for _ in range(6):
        try:
            lb.ask("p", model="mimo-v2.5", role="coder", step="",
                   provider="bifrost", provider_call=dead)
        except Exception:
            pass
    # tried only up to the threshold (2), then the open breaker skipped it
    assert calls["n"] == 2
