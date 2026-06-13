"""П7: the reviewer judges with ARTIFACTS, not just the spec text. It gets
the repository map (public surface of what already exists) and this node's
write_refused/rolled_back history, so architectural collisions are caught
BEFORE implementation, not after the integration goes red."""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import role_worker as rw  # noqa: E402
from harness import llm_log  # noqa: E402


# ─── llm_log.read_events ──────────────────────────────────────────────

def test_read_events_filters_by_kind_and_node(tmp_path, monkeypatch):
    log = tmp_path / "llm.jsonl"
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(log))
    llm_log.log({"event": "write_refused", "node": "cart", "path": "src/app.py",
                 "reason": "platform"})
    llm_log.log({"event": "call_start", "node": "cart"})
    llm_log.log({"event": "write_refused", "node": "pay", "path": "src/db.py",
                 "reason": "internals"})
    refusals = llm_log.read_events("write_refused")
    assert len(refusals) == 2
    cart_only = llm_log.read_events({"write_refused", "rolled_back"}, node="cart")
    assert len(cart_only) == 1 and cart_only[0]["path"] == "src/app.py"


def test_read_events_empty_when_log_unset(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_LLM_LOG", raising=False)
    assert llm_log.read_events("write_refused") == []


# ─── repo-map artifact ────────────────────────────────────────────────

def test_review_repo_map_lists_existing_surface(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cart.py").write_text(
        "def add_item(payload, query):\n    return {}\n", encoding="utf-8")
    digest = rw._review_repo_map(str(tmp_path))
    assert "REPOSITORY MAP" in digest
    assert "cart.py" in digest and "add_item" in digest


def test_review_repo_map_empty_without_root():
    assert rw._review_repo_map(None) == ""
    assert rw._review_repo_map("") == ""


# ─── refusal-history artifact ─────────────────────────────────────────

def test_review_refusals_renders_node_history(tmp_path, monkeypatch):
    log = tmp_path / "llm.jsonl"
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(log))
    llm_log.log({"event": "write_refused", "node": "seller", "path": "src/db.py",
                 "reason": "touches platform internals"})
    txt = rw._review_refusals("seller")
    assert "REFUSAL HISTORY" in txt
    assert "src/db.py" in txt and "platform internals" in txt


def test_review_refusals_empty_for_clean_node(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(tmp_path / "llm.jsonl"))
    assert rw._review_refusals("untouched") == ""
    assert rw._review_refusals("?") == ""
    assert rw._review_refusals(None) == ""


# ─── the artifacts reach the prompt ───────────────────────────────────

def test_reviewer_prompt_includes_artifacts(tmp_path, monkeypatch):
    # a workspace with an existing module + a refusal for the node under review
    ws = tmp_path / "wk"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "existing.py").write_text(
        "def handler(payload, query):\n    return {}\n", encoding="utf-8")
    spec = ws / "specs"
    spec.mkdir()
    (spec / "dup.md").write_text("# spec\n## Requirements\n- REQ-1\n",
                                 encoding="utf-8")
    log = tmp_path / "llm.jsonl"
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(log))
    llm_log.log({"event": "write_refused", "node": "dup", "path": "src/app.py",
                 "reason": "platform-seeded skeleton is immutable"})

    captured = {}

    def fake_call(prompt, **kw):
        captured["prompt"] = prompt
        return '{"verdict": "PASS", "reasons": []}'

    monkeypatch.setattr(rw, "_call_model", fake_call)
    monkeypatch.setattr(rw, "_chat_only", lambda: False)
    review = rw.make_reviewer()
    review({"spec": "specs/dup.md", "node": "dup", "depth": 1,
            "workspace_root": str(ws), "goal": "g", "constitution": []})
    p = captured["prompt"]
    assert "REPOSITORY MAP" in p and "existing.py" in p
    assert "REFUSAL HISTORY" in p and "src/app.py" in p
