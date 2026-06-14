"""Run administration from the dashboard: POST /api/run/select pins which
run the whole dashboard shows (fixing the "newest-by-mtime shows a dead
duplicate" trap), and POST /api/run/delete archives a run by moving it into
runs-out/_archive/ (reversible, never an on-disk erase). Both validate the
run name as a direct child of runs-out — the traversal guard."""
import io
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _mkrun(out, name):
    d = out / name
    (d / "hitl").mkdir(parents=True)
    (d / "trace.jsonl").write_text('{"tick":1}\n', encoding="utf-8")
    return d


class _FakeHandler(dash._H):
    """Drive do_POST without a socket."""
    def __init__(self, path, payload):
        self.path = path
        body = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent = {}

    def _send(self, code, ctype, body):
        self.sent = {"code": code, "body": body}


def _post(path, payload):
    h = _FakeHandler(path, payload)
    h.do_POST()
    return h.sent["code"], json.loads(h.sent["body"])


def _isolate(tmp_path, monkeypatch):
    out = tmp_path / "runs-out"
    out.mkdir()
    monkeypatch.setattr(dash, "OUT_DIR", out)
    dash._H.run_dir_override = None          # reset class-level pin
    return out


# ── select ────────────────────────────────────────────────────────────────

def test_select_pins_run(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    code, body = _post("/api/run/select", {"run": r.name})
    assert code == 200 and body["selected"] == r.name
    assert dash._H.run_dir_override == r


def test_select_empty_clears_pin(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    dash._H.run_dir_override = r
    code, body = _post("/api/run/select", {"run": ""})
    assert code == 200 and body["selected"] is None
    assert dash._H.run_dir_override is None


def test_select_unknown_run_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    code, body = _post("/api/run/select", {"run": "no-such-run__v001__x"})
    assert code == 400 and not body["ok"]


def test_select_traversal_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for bad in ("../etc", "a/b", "..", ".hidden"):
        code, body = _post("/api/run/select", {"run": bad})
        assert code == 400 and not body["ok"], bad


# ── delete = archive ────────────────────────────────────────────────────────

def test_delete_archives_dead_run(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    code, body = _post("/api/run/delete", {"run": r.name})
    assert code == 200 and body["archived"] == r.name
    # moved, not erased
    assert not r.exists()
    assert (out / "_archive" / r.name).is_dir()
    assert (out / "_archive" / r.name / "trace.jsonl").exists()


def test_delete_refuses_live_run(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    (r / "run.pid").write_text(str(os.getpid()) + "\n")  # this process = alive
    code, body = _post("/api/run/delete", {"run": r.name})
    assert code == 409 and body["error"] == "run is alive"
    assert r.exists()                                    # untouched


def test_delete_refuses_selected_run(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    dash._H.run_dir_override = r
    code, body = _post("/api/run/delete", {"run": r.name})
    assert code == 409 and body["error"] == "run is selected"
    assert r.exists()


def test_delete_traversal_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    code, body = _post("/api/run/delete", {"run": "../../etc/passwd"})
    assert code == 400 and not body["ok"]


def test_archive_dir_not_a_run(tmp_path, monkeypatch):
    # _archive and the counter file must never be treated as runs
    out = _isolate(tmp_path, monkeypatch)
    (out / "_archive").mkdir()
    (out / ".run-counter-p4").write_text("5")
    assert dash._resolve_run("_archive") is None
    assert dash._resolve_run(".run-counter-p4") is None
    assert dash._latest_run() is None        # neither is a run


# ── latest-run selection ignores side folders ──────────────────────────────

def test_latest_run_skips_archive(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    r = _mkrun(out, "2026-06-14T01-00-00__v002__p4-b2b-marketplace")
    arch = out / "_archive"
    arch.mkdir()
    # _archive is created later (newer mtime) but must NOT win
    assert dash._latest_run() == r
