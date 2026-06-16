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
import time as _time

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


# ── auto-follow picks the LIVE run, not newest-mtime ────────────────────────

def test_latest_run_prefers_alive_over_newer_dead(tmp_path, monkeypatch):
    # The bug: a dead older run whose dir was mtime-bumped after a newer run
    # started was shown as 'live' because selection was pure newest-mtime.
    # Liveness is now namespace-proof (_run_active: no SUMMARY.md + fresh trace),
    # NOT a run.pid os.kill — a finished run stays dead even if a leftover small
    # run.pid happens to match a live host PID (the v022 false-'alive').
    out = _isolate(tmp_path, monkeypatch)
    live = _mkrun(out, "2026-06-16T16-22-52__v020__p6-micro-notes")     # no SUMMARY
    dead = _mkrun(out, "2026-06-16T13-13-55__v018__p6-micro-notes")
    (dead / "SUMMARY.md").write_text("done\n", encoding="utf-8")        # finished
    (dead / "run.pid").write_text("1", encoding="utf-8")               # tiny PID: a
    #                                  live host process — must NOT read as alive
    os.utime(dead, None)               # and make the DEAD run newer by mtime
    assert dash._latest_run() == live


def test_run_active_is_namespace_proof(tmp_path, monkeypatch):
    # A run with a finished SUMMARY is never active; a fresh trace with no
    # SUMMARY is active; a stale trace (past the freshness window) is not —
    # regardless of any run.pid value.
    out = _isolate(tmp_path, monkeypatch)
    fresh = _mkrun(out, "2026-06-16T16-00-00__v001__p6-micro-notes")
    (fresh / "run.pid").write_text("22", encoding="utf-8")     # bogus tiny PID
    assert dash._run_active(fresh) is True

    done = _mkrun(out, "2026-06-16T16-00-00__v002__p6-micro-notes")
    (done / "SUMMARY.md").write_text("done\n", encoding="utf-8")
    assert dash._run_active(done) is False

    stale = _mkrun(out, "2026-06-16T16-00-00__v003__p6-micro-notes")
    old = _time.time() - dash._RUN_FRESH_S - 10
    os.utime(stale / "trace.jsonl", (old, old))
    assert dash._run_active(stale) is False


def test_latest_run_newest_when_none_alive(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    old = _mkrun(out, "2026-06-16T10-00-00__v001__p6-micro-notes")
    new = _mkrun(out, "2026-06-16T11-00-00__v002__p6-micro-notes")
    os.utime(old, (1000, 1000))               # explicitly older
    os.utime(new, (2000, 2000))               # newest mtime, both dead
    assert dash._latest_run() == new


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
