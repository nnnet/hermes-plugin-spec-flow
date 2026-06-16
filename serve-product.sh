#!/usr/bin/env bash
# Serve a spec-flow BUILT product (the WSGI app a run assembled) and curl-check
# it. Usage:
#   ./serve-product.sh [RUN_DIR] [PORT]
# RUN_DIR defaults to the newest p6 run under tests/runs-out/; PORT defaults to
# 8099 (override with the 2nd arg or PORT=...). It discovers the module that
# exposes a WSGI callable (wsgi_app/application/app), boots it on a fresh temp
# sqlite DB via the stdlib wsgiref server, prints WHERE it is serving, then runs
# the verification curls below and shows their output.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_DIR="${1:-$(ls -dt "$HERE"/tests/runs-out/*__v*__p6-micro-notes 2>/dev/null | head -1)}"
WS="$RUN_DIR/workspace"
DB="$(mktemp -u)"   # fresh path; the app's db layer creates it on first connect

# Port: start from the requested one (2nd arg / PORT, default 8099) and scan
# upward to the first FREE port — never curl a stranger's server on a busy port.
want="${2:-${PORT:-8099}}"
PORT=""
for p in $(seq "$want" $((want + 40))); do
  if ! { exec 3<>"/dev/tcp/127.0.0.1/$p"; } 2>/dev/null; then PORT="$p"; break; fi
  exec 3>&- 2>/dev/null || true
done
[ -n "$PORT" ] || { echo "no free port near $want" >&2; exit 1; }
[ "$PORT" = "$want" ] || echo "(port $want busy → using $PORT)"

if [ ! -d "$WS/src" ]; then
  echo "no workspace/src in: $RUN_DIR" >&2; exit 1
fi
echo "run dir : $RUN_DIR"
echo "src     : $WS/src"
echo

# ── boot the assembled WSGI app in the background ─────────────────────────────
NOTES_DB="$DB" MARKETPLACE_DB="$DB" python3 - "$WS/src" "$PORT" <<'PY' &
import sys, os, glob, pathlib, importlib
src, port = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, src)
app = None
for f in sorted(glob.glob(os.path.join(src, "*.py"))):
    name = pathlib.Path(f).stem
    if name == "__init__":
        continue
    try:
        m = importlib.import_module(name)   # src on sys.path → siblings resolve
    except Exception as e:                  # noqa: BLE001
        print(f"  (skip {name}: {e})", file=sys.stderr)
        continue
    for attr in ("wsgi_app", "application", "app"):
        c = getattr(m, attr, None)
        if callable(c):
            app, where = c, f"{name}.{attr}"
            break
    if app:
        break
if app is None:
    print("NO WSGI CALLABLE FOUND in src/*.py", file=sys.stderr)
    sys.exit(1)
from wsgiref.simple_server import make_server
print(f"SERVING {where}  ->  http://127.0.0.1:{port}", flush=True)
make_server("127.0.0.1", port, app).serve_forever()
PY
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait until OUR child is actually listening; bail loudly if it died (e.g. no
# WSGI callable, import error) instead of curling whatever else holds the port.
BASE="http://127.0.0.1:${PORT}"
for _ in $(seq 1 40); do
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "server process exited before binding" >&2; exit 1; }
  curl -s -o /dev/null --noproxy '*' "${BASE}/health" && break
  sleep 0.25
done
echo "================  app up at ${BASE}  ================"
echo

c() { echo "\$ $*"; eval "$@"; echo; echo; }

c "curl -s -i --noproxy '*' ${BASE}/health"
c "curl -s --noproxy '*' -X POST ${BASE}/notes -H 'Content-Type: application/json' -d '{\"text\":\"hello from curl\"}'"
c "curl -s --noproxy '*' -X POST ${BASE}/notes -H 'Content-Type: application/json' -d '{\"text\":\"second note\"}'"
c "curl -s --noproxy '*' ${BASE}/notes"
c "curl -s --noproxy '*' ${BASE}/ui | head -c 600"

echo "================  done; stopping server (pid ${SERVER_PID})  ================"
