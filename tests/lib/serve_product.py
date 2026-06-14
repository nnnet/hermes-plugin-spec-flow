"""Serve a run's BUILT PRODUCT (the workspace app) over HTTP.

The plugin's deliverable at depth=execute is a working WSGI application in
``<run>/workspace``. This runner mounts it as-is — the product is not
modified — and only adds a landing page at ``/`` documenting the frozen
API contract, so a browser visitor knows what to call.

Usage:
    python3 tests/serve_product.py --workspace <run>/workspace --port 8508
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from wsgiref.simple_server import make_server

_LANDING = """<!doctype html><meta charset=utf-8>
<title>spec-flow built product</title>
<body style="font-family:monospace;background:#0d1117;color:#c9d1d9;padding:24px">
<h2>🛒 B2B marketplace MVP — built by the spec-flow plugin</h2>
<p>Это живое приложение, СОБРАННОЕ плагином (workspace прогона, без правок).
Прошло приёмку: сквозной смоук зелёный на корневой интеграции.</p>
<h3>API (замороженный контракт):</h3><pre>
POST /sellers   {"name": "...", "email": "..."}            → 201 {id, kyc_status}
POST /products  {"seller_id": N, "title": "...",
                 "price_cents": N}                          → 201 {id, ...}
GET  /products?search=строка                                → 200 {items: [...]}
POST /orders    {"product_id": N, "buyer_email": "..."}     → 201 {id, status: paid}
GET  /payouts?seller_id=N                                   → 200 {items: [...]}
</pre>
<h3>Попробовать (с хоста):</h3><pre>
curl -s -X POST http://HOST:PORT/sellers -d '{"name":"Demo","email":"d@x.io"}'
curl -s -X POST http://HOST:PORT/products -d '{"seller_id":1,"title":"Rack server","price_cents":49900}'
curl -s 'http://HOST:PORT/products?search=rack'
curl -s -X POST http://HOST:PORT/orders -d '{"product_id":1,"buyer_email":"b@y.io"}'
curl -s 'http://HOST:PORT/payouts?seller_id=1'
</pre></body>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True,
                    help="run workspace dir (contains src/app.py)")
    ap.add_argument("--port", type=int, default=8508)
    ap.add_argument("--db", default="",
                    help="sqlite path (default: a fresh temp file)")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    sys.path.insert(0, str(ws / "src"))
    os.environ["MARKETPLACE_DB"] = args.db or os.path.join(
        tempfile.mkdtemp(prefix="specflow-product-"), "marketplace.db")

    import app  # noqa: E402 — the product's own entry point

    landing = _LANDING.replace("HOST:PORT", f"<host>:{args.port}")

    def wrapped(environ, start_response):
        if environ.get("PATH_INFO", "/") == "/" \
                and environ["REQUEST_METHOD"] == "GET":
            data = landing.encode("utf-8")
            start_response("200 OK",
                           [("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Length", str(len(data)))])
            return [data]
        return app.wsgi_app(environ, start_response)

    print(f"product from {ws}\ndb at {os.environ['MARKETPLACE_DB']}\n"
          f"serving on 0.0.0.0:{args.port}")
    make_server("0.0.0.0", args.port, wrapped).serve_forever()


if __name__ == "__main__":
    main()
