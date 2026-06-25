"""When the case declares no explicit entrypoint but the human spec declares a
runnable web product, _product_check must use the engine's own boot oracle
(_assembled_product_boots) to REALLY serve + probe the assembled entry, rather
than dead-ending on 'no runnable entrypoint'. Regression for v061: pytest green,
product wrongly NOT READY only because the case had no entrypoint."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from spec_flow_runner import Engine, DEPTH_PRODUCT  # noqa: E402


class _WS:
    enabled = True
    root = "/tmp/ws-x"

    def __init__(self):
        self.written = {}

    def _write(self, name, content, tag=""):
        self.written[name] = content


def _eng(boots):
    e = Engine.__new__(Engine)
    e.depth = DEPTH_PRODUCT
    e.workspace = _WS()
    e._product_contract = lambda: {"entry": "src/app.py", "callable": ["wsgi_app"]}
    e._assembled_product_boots = lambda: boots
    e.emit = lambda *a, **k: None
    return e


_ACCEPT = {"smoke": ["service starts and answers GET /health -> 200"],
           "e2e": ["POST /notes {text} -> {id}; GET /notes -> items"]}


def test_boot_oracle_makes_product_ready_without_entrypoint():
    e = _eng((True, "all routes answered"))
    e._product_check(_ACCEPT)
    md = e.workspace.written["PRODUCT-RESULTS.md"]
    assert "READY" in md and "NOT READY" not in md
    assert "boots and serves its contract" in md


def test_boot_oracle_fails_when_product_does_not_serve():
    e = _eng((False, "GET /notes -> 404"))
    e._product_check(_ACCEPT)
    md = e.workspace.written["PRODUCT-RESULTS.md"]
    assert "NOT READY" in md
    assert "404" in md


def test_no_contract_still_needs_entrypoint():
    # a non-web product (no derivable contract) with no entrypoint stays honest:
    # readiness cannot be asserted at runtime.
    e = _eng((True, ""))
    e._product_contract = lambda: {}
    e._product_check(_ACCEPT)
    md = e.workspace.written["PRODUCT-RESULTS.md"]
    assert "NOT READY" in md
    assert "no runnable entrypoint" in md


# ── boot-gate robustness: a malformed body must be 4xx, never a 5xx crash ───
import pathlib                                              # noqa: E402

_APP_HEAD = '''import json
_NOTES = []
def wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    if path == "/health" and method == "GET":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b"{}"]
    if path == "/notes" and method == "POST":
        n = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(n)
%s
        _NOTES.append(data.get("text", ""))
        start_response("201 Created", [("Content-Type", "application/json")])
        return [json.dumps({"id": len(_NOTES)}).encode()]
    if path == "/notes" and method == "GET":
        items = [{"id": i + 1, "text": t} for i, t in enumerate(_NOTES)][::-1]
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"items": items}).encode()]
    start_response("404 Not Found", [])
    return [b""]
'''
# unguarded: json.loads raises on a malformed body -> 500 (the v085 wart)
_APP_500 = _APP_HEAD % "        data = json.loads(raw)"
# guarded: a malformed body is rejected with 400 (a real product)
_APP_400 = _APP_HEAD % (
    "        try:\n"
    "            data = json.loads(raw)\n"
    "        except Exception:\n"
    '            start_response("400 Bad Request", [])\n'
    "            return [b'bad json']")


def _boot_eng(tmp_path, app_src):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(app_src, encoding="utf-8")
    e = Engine.__new__(Engine)
    e.workspace = type("W", (), {"enabled": True, "root": str(root)})()
    e._product_contract = lambda: {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"ok_route": "/health", "json_roundtrip": "/notes"}}
    return e


def test_boot_gate_rejects_malformed_body_that_500s(tmp_path):
    ok, detail = _boot_eng(tmp_path, _APP_500)._assembled_product_boots()
    assert ok is False, "an app that 500s on a malformed body must NOT be READY"
    assert "malformed" in detail.lower()


def test_boot_gate_accepts_malformed_body_handled_as_400(tmp_path):
    ok, detail = _boot_eng(tmp_path, _APP_400)._assembled_product_boots()
    assert ok is True, detail
