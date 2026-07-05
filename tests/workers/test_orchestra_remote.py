"""Phase 3 — a specialist on a REMOTE provider routes its orchestra step through
the provider adapter (fake transport) instead of the local LLM, and the
orchestra still completes.

The ONLY remote contact is a fake transport injected via ``rw.PROVIDER_TRANSPORT``.
Everything else runs for REAL: a real git workspace, real file writes and a real
pytest leaf bar — the adapter (or, on fallback, the local server) returns a
RUNNABLE green leaf, so the verdict is a genuine pytest run, never a stub.

This drives the REAL ``_call_model`` dispatch so the provider seam is exercised
end to end: a ``provider: hermes`` coder's reply comes from the adapter, the
orchestra writes its files and verifies the leaf green — _orchestra_run is NOT
edited.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb   # noqa: E402
from harness import ws_tx               # noqa: E402
from harness_fakeapi import ok          # noqa: E402


# A real green leaf the orchestra can write and pass under a genuine pytest run.
_SRC = "def leaf1():\n    return {n}\n"
_TEST = ("import sys, pathlib\n"
         "sys.path.insert(0, str(pathlib.Path(__file__).resolve()"
         ".parents[1] / 'src'))\n"
         "import leaf1\n"
         "def test_leaf1():\n    assert leaf1.leaf1() == {n}\n")


def _green(n):
    return {"src/leaf1.py": _SRC.format(n=n),
            "tests/test_leaf1.py": _TEST.format(n=n)}


class _RealWS:
    """A real workspace: writes land on disk so the orchestra's transaction,
    pytest run and leaf bar all execute against real files."""
    enabled = True

    def __init__(self, root):
        self.root = str(root)

    def _write(self, rel, body, kind):
        f = pathlib.Path(self.root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
        return rel


def _ctx(tmp_path):
    spec = pathlib.Path(tmp_path) / "specs" / "leaf1.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Leaf One\nReturn a constant.\n", encoding="utf-8")
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": _RealWS(tmp_path), "spec": "specs/leaf1.md",
            "module": "leaf1", "specialty": ""}


def _common(monkeypatch, tmp_path):
    """Pin only the config seams; leave git/pytest/file writes REAL."""
    ws_tx.ensure_repo(str(tmp_path))
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_ensemble_size", lambda *a, **k: 1)


def test_remote_coder_step_routes_through_adapter(monkeypatch, tmp_path,
                                                  fake_openai):
    # team: a single coder on the hermes provider with its adapter config
    team = [{"role": "coder", "provider": "hermes",
             "gateway": "https://hx.example", "agent": "senior-dev"}]
    lb.configure_workers({"implementer": {"team": {"specialists": team}}})
    _common(monkeypatch, tmp_path)

    # point the real LLM door at a REAL local server: for a remote specialist
    # the local LLM must NEVER be called, so this server must receive zero hits.
    srv = fake_openai([(200, ok("must-not-be-used"))])

    sent = []

    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        sent.append({"url": url, "method": method,
                     "body": json.loads(body) if body else None})
        # the gateway answers as an OpenAI chat completion whose assistant
        # content carries the files object (46cfa6a: the Hermes gateway IS its
        # api_server — /v1/agents/... does not exist)
        return 200, json.dumps({"choices": [{"message": {
            "content": json.dumps({"files": _green(1)})}}]})
    monkeypatch.setattr(rw, "PROVIDER_TRANSPORT", transport)

    rw._orchestra_run(_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    lb.configure_workers(None)

    # the coder step hit the Hermes adapter (not the LLM) ...
    assert sent and sent[0]["method"] == "POST"
    assert sent[0]["url"] == "https://hx.example/v1/chat/completions"
    # ... framed as a real agent invocation: the system message names the
    # agent + role, the user message carries the node's task
    msgs = sent[0]["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert "senior-dev" in msgs[0]["content"] and "coder" in msgs[0]["content"]
    assert msgs[1]["role"] == "user" and "leaf1" in msgs[1]["content"]
    # ... and the adapter's files were written for real by the orchestra ...
    body = (tmp_path / "src" / "leaf1.py").read_text(encoding="utf-8")
    assert "def leaf1():\n    return 1\n" in body
    # ... and the local LLM door was genuinely never touched
    assert srv.call_count == 0, "local LLM was hit for a remote specialist"


def test_remote_specialist_falls_back_to_local_when_unreachable(monkeypatch, tmp_path,
                                                                fake_openai):
    # A remote specialist whose service is DOWN must DEGRADE to its local model,
    # not fail the leaf — and the degradation is recorded as an llm_fallback hop.
    team = [{"role": "coder", "provider": "hermes",
             "gateway": "https://hx.example", "agent": "senior-dev",
             "model": "openrouter/qwen/qwen3-coder:free"}]
    lb.configure_workers({"implementer": {"team": {"specialists": team}}})
    _common(monkeypatch, tmp_path)
    # the local fallback path is the chat backend (NOT the claude CLI subprocess)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(
        rw.llm_backend, "chain_for",
        lambda *a, **k: ["openrouter/qwen/qwen3-coder:free"])

    # local LLM IS the fallback target — the REAL door hits a real local server
    # (the specialist's model is a free id, so the free-only gate lets it run),
    # returning a runnable green leaf that passes a genuine pytest run.
    srv = fake_openai([(200, ok(json.dumps({"files": _green(2)})))])

    # the remote transport is DOWN (HTTP 500) → adapter raises → fallback fires
    monkeypatch.setattr(rw, "PROVIDER_TRANSPORT",
                        lambda url, **k: (500, '{"error": "upstream down"}'))

    events = []
    _orig_log = rw.llm_log.log
    monkeypatch.setattr(rw.llm_log, "log",
                        lambda e: events.append(e) or _orig_log(e))

    rw._orchestra_run(_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    lb.configure_workers(None)

    assert srv.call_count >= 1, "remote down must fall back to the local model"
    body = (tmp_path / "src" / "leaf1.py").read_text(encoding="utf-8")
    assert "def leaf1():\n    return 2\n" in body
    # Phase 2: the provider is the chain's first link inside the single door, so
    # the degradation is one of ask()'s normal llm_fallback hops (carrying the
    # provider name), not a private provider_fallback bypass event.
    assert any(e.get("event") == "llm_fallback" and e.get("provider") == "hermes"
               and e.get("from_model", "").startswith("hermes:")
               for e in events), "the degradation must be logged as an llm_fallback hop"
