"""Phase 3 — a specialist on a REMOTE provider routes its orchestra step through
the provider adapter (fake transport) instead of the local LLM, and the
orchestra still completes.

Hermetic: NO network, NO live LLM, NO pytest subprocess. The ONLY remote contact
is a fake transport injected via ``rw.PROVIDER_TRANSPORT``; the orchestra's
file/git/verify building blocks are stubbed exactly as in test_orchestra.

This drives the REAL ``_call_model`` dispatch (not stubbed) so the provider seam
is exercised end to end: a ``provider: hermes`` coder's reply comes from the
adapter, the orchestra writes its files and finishes green — _orchestra_run is
NOT edited.
"""
import contextlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb   # noqa: E402


@contextlib.contextmanager
def _noop_tx(*a, **k):
    yield object()


def _ctx(tmp_path):
    ws = type("WS", (), {"root": str(tmp_path),
                         "_write": lambda self, *a, **k: None})()
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": ws, "spec": "specs/leaf1.md", "module": "leaf1",
            "specialty": ""}


def test_remote_coder_step_routes_through_adapter(monkeypatch, tmp_path):
    # team: a single coder on the hermes provider with its adapter config
    team = [{"role": "coder", "provider": "hermes",
             "gateway": "https://hx.example", "agent": "senior-dev"}]
    lb.configure_workers({"implementer": {"team": {"specialists": team}}})
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC BODY")
    monkeypatch.setattr(rw, "_ensemble_size", lambda: 1)
    # the orchestra building blocks (same stubs as test_orchestra)
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: (True, "green"))

    captured = {}

    def _write(ws, files, fn):
        captured["files"] = files
        return bool(files)
    monkeypatch.setattr(rw, "_write_reply_files", _write)

    # the fake transport: the local LLM must NEVER be called for this step
    def _no_llm(*a, **k):
        raise AssertionError("local LLM called for a remote specialist")
    monkeypatch.setattr(lb, "ask", _no_llm)

    sent = []

    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        sent.append({"url": url, "method": method,
                     "body": json.loads(body) if body else None})
        return 200, json.dumps({"files": {"src/leaf1.py": "def leaf1():\n    return 1\n"}})
    monkeypatch.setattr(rw, "PROVIDER_TRANSPORT", transport)

    rw._orchestra_run(_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    lb.configure_workers(None)

    # the coder step hit the Hermes adapter (not the LLM) ...
    assert sent and sent[0]["method"] == "POST"
    assert sent[0]["url"] == "https://hx.example/v1/agents/senior-dev/messages"
    assert sent[0]["body"]["role"] == "coder"
    # ... and the adapter's files were written by the orchestra
    assert captured["files"] == {"src/leaf1.py": "def leaf1():\n    return 1\n"}


def test_remote_specialist_falls_back_to_local_when_unreachable(monkeypatch, tmp_path):
    # A remote specialist whose service is DOWN must DEGRADE to its local model,
    # not fail the leaf — and the degradation is recorded as provider_fallback.
    team = [{"role": "coder", "provider": "hermes",
             "gateway": "https://hx.example", "agent": "senior-dev",
             "model": "openrouter/qwen/qwen3-coder:free"}]
    lb.configure_workers({"implementer": {"team": {"specialists": team}}})
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC BODY")
    monkeypatch.setattr(rw, "_ensemble_size", lambda: 1)
    # the local fallback path is the chat backend (NOT the claude CLI subprocess)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: (True, "green"))

    captured = {}
    monkeypatch.setattr(rw, "_write_reply_files",
                        lambda ws, files, fn: captured.update(files=files) or bool(files))

    # local LLM IS the fallback target — it must be called and produce the files
    local_called = {"n": 0}

    def _local_ask(prompt, **k):
        local_called["n"] += 1
        return '{"files": {"src/leaf1.py": "def leaf1():\\n    return 2\\n"}}'
    monkeypatch.setattr(lb, "ask", _local_ask)

    # the remote transport is DOWN (HTTP 500) → adapter raises → fallback fires
    monkeypatch.setattr(rw, "PROVIDER_TRANSPORT",
                        lambda url, **k: (500, '{"error": "upstream down"}'))

    events = []
    monkeypatch.setattr(rw.llm_log, "log", lambda e: events.append(e))

    rw._orchestra_run(_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    lb.configure_workers(None)

    assert local_called["n"] >= 1, "remote down must fall back to the local model"
    assert captured.get("files") == {"src/leaf1.py": "def leaf1():\n    return 2\n"}
    assert any(e.get("event") == "provider_fallback" and e.get("provider") == "hermes"
               for e in events), "the degradation must be logged as provider_fallback"
