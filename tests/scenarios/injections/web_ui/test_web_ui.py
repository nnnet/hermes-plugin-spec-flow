"""ACCEPTANCE for the standing human requirement 'web_ui' (added mid-run).

The assembled product must serve a server-rendered HTML notes page at GET /ui:
the page lists existing notes and carries an add form (a text input named
"text"). Behavioural only — it never dictates the implementation. Gates ONLY
the root integrate (this file lives in tests/smoke/, platform-protected)."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTES_DB", str(tmp_path / "notes.db"))


def _call(method, path, body=b"", query=""):
    import app
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body)}
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])
        captured["ctype"] = dict(headers).get("Content-Type", "")

    chunks = app.wsgi_app(environ, start_response)
    payload = b"".join(chunks) if chunks else b""
    return captured.get("status"), captured.get("ctype", ""), payload.decode("utf-8", "replace")


def test_ui_page_serves_html():
    status, ctype, html = _call("GET", "/ui")
    assert status == 200
    assert "text/html" in ctype
    assert "<form" in html.lower()
    # the add form exposes a text input named "text"
    assert 'name="text"' in html or "name='text'" in html


def test_ui_lists_an_existing_note():
    import json as _json
    # create a note through the API, then it must appear on the page
    body = _json.dumps({"text": "buy milk"}).encode()
    status, _, _ = _call("POST", "/notes", body)
    assert status in (200, 201)
    status, _, html = _call("GET", "/ui")
    assert status == 200 and "buy milk" in html
