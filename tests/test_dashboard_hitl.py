"""Bidirectional HITL channel for the dashboard ✋ tab: _hitl_state reads
the worker→human asks + answer-pending flag + injected requirements; the
POST endpoints (answer/inject) write the same artifacts the cron operator
writes by hand."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _run(tmp_path):
    d = tmp_path / "run"
    (d / "hitl").mkdir(parents=True)
    return d


def test_hitl_state_none_run():
    assert dash._hitl_state(None) == {"empty": True}


def test_hitl_state_parses_asks_and_answers(tmp_path):
    d = _run(tmp_path)
    (d / "hitl" / "questions.md").write_text(
        "## [12:00] implementer @ cart asks: which route?\n"
        "**answer (auto/routing):** use query params\n"
        "## [12:05] implementer @ pay asks: where is the spec?\n",
        encoding="utf-8")
    st = dash._hitl_state(d)
    assert any("cart" in a for a in st["asks"])
    assert any("pay" in a for a in st["asks"])
    assert any("auto/routing" in a for a in st["answered"])
    assert st["pending"] is False


def test_hitl_state_pending_when_answer_present(tmp_path):
    d = _run(tmp_path)
    (d / "hitl" / "answer.md").write_text("record it\n", encoding="utf-8")
    assert dash._hitl_state(d)["pending"] is True
    # empty answer file is NOT pending
    (d / "hitl" / "answer.md").write_text("   \n", encoding="utf-8")
    assert dash._hitl_state(d)["pending"] is False


def test_hitl_state_lists_injected_requirements(tmp_path):
    d = _run(tmp_path)
    wreq = d / "hitl" / "requirements" / "web_ui"
    wreq.mkdir(parents=True)
    (wreq / "REQUIREMENT.md").write_text("serve HTML pages\n", encoding="utf-8")
    st = dash._hitl_state(d)
    names = [r["name"] for r in st["requirements"]]
    assert "web_ui" in names
    assert "serve HTML" in st["requirements"][0]["body"]


# ─── POST endpoints write the right artifacts ─────────────────────────

class _FakeHandler(dash._H):
    """Drive do_POST without a socket: stub the request plumbing."""
    def __init__(self, run_dir, path, payload):
        self._rd = run_dir
        self.path = path
        self._payload = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self._payload))}
        self.sent = {}
        import io
        self.rfile = io.BytesIO(self._payload)

    def _run_dir(self):
        return self._rd

    def _send(self, code, ctype, body):
        self.sent = {"code": code, "body": body}


def test_post_answer_writes_answer_md(tmp_path):
    d = _run(tmp_path)
    h = _FakeHandler(d, "/api/hitl/answer", {"text": "proceed: record a spec"})
    h.do_POST()
    assert h.sent["code"] == 200
    assert (d / "hitl" / "answer.md").read_text().strip() == "proceed: record a spec"


def test_post_answer_rejects_empty(tmp_path):
    d = _run(tmp_path)
    h = _FakeHandler(d, "/api/hitl/answer", {"text": "   "})
    h.do_POST()
    assert h.sent["code"] == 400
    assert not (d / "hitl" / "answer.md").exists()


def test_post_inject_creates_requirement(tmp_path):
    d = _run(tmp_path)
    h = _FakeHandler(d, "/api/hitl/inject",
                     {"name": "web ui!", "text": "serve /ui/catalog"})
    h.do_POST()
    assert h.sent["code"] == 200
    # name is sanitized to a safe folder
    req = d / "hitl" / "requirements" / "web_ui"
    assert req.is_dir()
    assert "serve /ui/catalog" in (req / "REQUIREMENT.md").read_text()


def test_post_inject_requires_name_and_text(tmp_path):
    d = _run(tmp_path)
    h = _FakeHandler(d, "/api/hitl/inject", {"name": "", "text": "x"})
    h.do_POST()
    assert h.sent["code"] == 400


# ─── run control: stop / start endpoints ──────────────────────────────

def test_post_run_stop_drops_sentinel(tmp_path):
    d = _run(tmp_path)
    (d / "workspace").mkdir()
    h = _FakeHandler(d, "/api/run/stop", {})
    h.do_POST()
    assert h.sent["code"] == 200
    # the STOP sentinel the engine polls at each node boundary is written
    assert (d / "workspace" / ".spec-flow" / "STOP").exists()


def test_post_run_stop_signals_pid(tmp_path, monkeypatch):
    d = _run(tmp_path)
    (d / "workspace").mkdir()
    (d / "run.pid").write_text("424242\n", encoding="utf-8")
    killed = {}
    import live_dashboard as ld
    monkeypatch.setattr(ld.os, "kill", lambda pid, sig: killed.setdefault("pid", pid))
    h = _FakeHandler(d, "/api/run/stop", {})
    h.do_POST()
    body = json.loads(h.sent["body"])
    assert body["ok"] and body["signalled"] == 424242
    assert killed["pid"] == 424242
