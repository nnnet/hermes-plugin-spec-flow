"""Memory providers: fake (offline stand-in) and Hindsight adapter.

The suite stays green WITHOUT the Hermes stack: the adapter is exercised
against a stubbed HTTP layer; failure tolerance is the key contract —
memory is seasoning, a dead memory service must not kill a run."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import memory as mem    # noqa: E402


# ─── bank naming ──────────────────────────────────────────────────────

def test_bank_names():
    assert mem.role_bank("implementer") == "specflow-role-implementer"
    assert mem.project_bank("p4 B2B marketplace!") == \
        "specflow-project-p4-b2b-marketplace"


# ─── fake provider (offline) ──────────────────────────────────────────

def test_fake_retain_recall_by_relevance():
    m = mem.FakeMemory()
    m.retain("b", "payouts must use amount_cents from the order row")
    m.retain("b", "search endpoint matches against title substring")
    hits = m.recall("b", "how do payouts compute amount?")
    assert hits and "amount_cents" in hits[0]


def test_fake_banks_are_isolated():
    m = mem.FakeMemory()
    m.retain("a", "fact about payouts")
    assert m.recall("b", "payouts") == []


def test_fake_recall_respects_budget():
    m = mem.FakeMemory()
    for i in range(5):
        m.retain("b", f"payouts rule number {i}: " + "x" * 300)
    hits = m.recall("b", "payouts rule", limit=5, budget_chars=700)
    assert 1 <= len(hits) <= 2
    assert sum(len(h) for h in hits) <= 700


def test_recall_block_empty_costs_nothing():
    m = mem.FakeMemory()
    assert m.recall_block("b", "anything") == ""
    m.retain("b", "constitution wins over memory")
    block = m.recall_block("b", "what wins, constitution or memory?")
    assert "RELEVANT EXPERIENCE" in block and "advisory" in block


# ─── hindsight adapter against a stubbed HTTP layer ───────────────────

class _StubHindsight(mem.HindsightMemory):
    def __init__(self, responses):
        super().__init__(base_url="http://stub:0")
        self.calls = []
        self._responses = list(responses)

    def _http_post(self, url, payload):
        self.calls.append((url, payload))
        return self._responses.pop(0)


def test_hindsight_retain_contract():
    h = _StubHindsight([(202, "{}")])
    ok = h.retain("specflow-role-implementer",
                  "**bold** fact about `payouts`",
                  context="leaf payouts", tags=["repair"])
    assert ok
    url, payload = h.calls[0]
    assert url.endswith("/v1/default/banks/specflow-role-implementer/memories")
    item = payload["items"][0]
    assert "*" not in item["content"] and "`" not in item["content"]
    assert item["context"] == "leaf payouts" and item["tags"] == ["repair"]
    assert payload["async"] is True


def test_hindsight_recall_contract_and_budget():
    body = json.dumps({"results": [
        {"text": "payouts read amount_cents from orders"},
        {"content": "alt key is also accepted"},
        {"text": ""},
    ]})
    h = _StubHindsight([(200, body)])
    hits = h.recall("specflow-project-p4", "payouts")
    assert hits == ["payouts read amount_cents from orders",
                    "alt key is also accepted"]
    url, payload = h.calls[0]
    assert url.endswith("/banks/specflow-project-p4/memories/recall")
    assert payload["query"] == "payouts"


def test_hindsight_failures_never_raise():
    class Down(mem.HindsightMemory):
        def _http_post(self, url, payload):
            raise OSError("connection refused")

    d = Down(base_url="http://127.0.0.1:1")
    assert d.retain("b", "x") is False
    assert d.recall("b", "x") == []
    h = _StubHindsight([(500, "boom")])
    assert h.recall("b", "x") == []


# ─── factory from the case YAML block ─────────────────────────────────

def test_factory_modes():
    assert mem.make_provider(None) is None
    assert mem.make_provider({}) is None
    assert isinstance(mem.make_provider({"provider": "fake"}),
                      mem.FakeMemory)
    h = mem.make_provider({"provider": "hindsight",
                           "url": "http://127.0.0.1:8888/"})
    assert isinstance(h, mem.HindsightMemory)
    assert h.base_url == "http://127.0.0.1:8888"
    with pytest.raises(ValueError, match="unknown memory provider"):
        mem.make_provider({"provider": "redis"})
