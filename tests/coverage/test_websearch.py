"""web_search remedy: offline-safe (no key => graceful empty, never raises)."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_websearch as ws        # noqa: E402


def test_no_backend_is_graceful(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_SEARCH_BACKEND", "none")
    out = ws.search("how to fix X")
    assert out["available"] is False
    assert out["results"] == []
    assert out["error"] == ""


def test_empty_query():
    out = ws.search("   ")
    assert out["available"] is False


def test_summarize_empty():
    assert "no results" in ws.summarize({"results": []})


def test_summarize_rows():
    r = {"results": [{"title": "T", "snippet": "S", "url": "u"}]}
    assert "T" in ws.summarize(r)


def test_unknown_backend_degrades(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_SEARCH_BACKEND", "bogus")
    out = ws.search("q")
    assert out["available"] is False
    assert "unknown backend" in out["error"]
