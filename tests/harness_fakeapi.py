"""A REAL local HTTP server that speaks the OpenAI chat-completions protocol.

This is NOT a monkeypatch and NOT a stub of our code: it is a test double of the
*remote API*, and the harness's real ``_ask_openai`` -> ``_http_post`` ->
``urllib.urlopen`` makes a real HTTP request to it over a real socket. Tests
point the backend at it through the real config seam (``llm_backend.BASE_URL``),
so the entire request/parse/retry/fallback/logging path runs for real — only the
upstream server's replies are scripted.

Usage::

    with FakeOpenAI([(200, ok("hi")), (429, "rate"), (200, ok("done"))]) as srv:
        old = lb.BASE_URL
        lb.BASE_URL = srv.base_url
        try:
            ... exercise lb.ask(...) ...
            assert srv.requests[0]["model"] == "..."
        finally:
            lb.BASE_URL = old
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def ok(text: str = "hello", usage: dict | None = None) -> str:
    """A 200 chat-completions body carrying ``text`` as the assistant reply."""
    body = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


class FakeOpenAI:
    """A scriptable OpenAI-compatible endpoint on an ephemeral localhost port.

    ``script`` is a list of ``(status, body)`` replies served in order; the last
    entry repeats once the list is exhausted. Every received request (its parsed
    JSON payload + path) is recorded in ``requests`` for assertions.
    """

    def __init__(self, script: list[tuple[int, str]], *, delay: float = 0.0):
        self._script = list(script) or [(200, ok())]
        self._i = 0
        self._delay = delay
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        # in-flight handler concurrency, so a real threaded server can measure
        # how many client calls were simultaneously on the wire (e.g. to prove
        # a client-side concurrency gate caps in-flight LLM requests).
        self._active = 0
        self.peak_concurrency = 0
        srv = self  # close over the instance for the handler

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence stderr access log
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n).decode("utf-8") if n else "{}"
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"_raw": raw}
                with srv._lock:
                    srv._active += 1
                    srv.peak_concurrency = max(srv.peak_concurrency,
                                               srv._active)
                    srv.requests.append(
                        {"path": self.path,
                         "model": payload.get("model"),
                         "messages": payload.get("messages"),
                         "payload": payload,
                         "auth": self.headers.get("Authorization")})
                    status, body = srv._next()
                try:
                    if srv._delay:
                        time.sleep(srv._delay)
                finally:
                    with srv._lock:
                        srv._active -= 1
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)

    def _next(self) -> tuple[int, str]:
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def __enter__(self) -> "FakeOpenAI":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
