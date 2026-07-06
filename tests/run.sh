#!/usr/bin/env bash
# spec-flow run launcher — ONE short command, minimal tokens.
#
# Wraps run-detached.sh with the mandatory smart defaults so a run never has to
# be spelled out by hand:
#   * FULLY DETACHED (setsid, survives /compact and the caller shell) — inherited
#     from run-detached.sh;
#   * VERSIONED run dir runs-out/<ts>__v<N>__<case> (run_cases assigns v<N>);
#   * PARALLEL software: --workers real (live parallel role workers) unless
#     overridden — the run must exercise the engine's parallel wave execution;
#   * DASHBOARD on a canonical port (default 8092); a stale holder of that port
#     is killed first so the URL stays stable;
#   * CHECKPOINTS on (--checkpoint-every 1) so a later-stage check can RESUME
#     from a previous run instead of paying for the early stages again.
#
# Usage (all positional after <case> are order-free):
#   tests/run.sh <case> [depth] [from=N] [resume=DIR] [port=P] [--any --extra]
#     <case>   substring of the case file (e.g. p6, p4, bat)
#     depth    one of spec|scaffold|verify|execute|product  (default: product)
#     from=N   resume from a previous run number vN (--from-run N)
#     resume=DIR  resume a specific run dir (--resume DIR)
#     port=P   dashboard port (default 8092)
#   anything else is passed verbatim to run_cases.py (e.g. --doctor-enabled,
#   --decomposer llm, --model ..., --from-checkpoint M).
#
# Examples:
#   tests/run.sh p6 spec               # fresh v<N>, specs only, parallel workers
#   tests/run.sh p6 product            # full depth
#   tests/run.sh p6 execute from=165   # resume v165 from its checkpoint, run execute
#   tests/run.sh p4 product --doctor-enabled
#
# HARD RULE (project): runs are launched ONLY through this script (or the
# run-detached.sh it wraps), never `python run_cases.py` in the caller session,
# never attached. Invoke it with the sandbox disabled (it needs setsid to
# survive and network for the live pool). Prefer resuming from a checkpoint when
# checking a LATE stage. See mem: run-detached / run-launcher rule.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ $# -ge 1 ] || { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

CASE="$1"; shift

DEPTH="product"
PORT="${SPEC_FLOW_DASH_PORT:-8092}"
WORKERS="real"
FROM=()
EXTRA=()

for a in "$@"; do
  case "$a" in
    spec|scaffold|verify|execute|product) DEPTH="$a" ;;
    from=*)    FROM=(--from-run "${a#from=}") ;;
    resume=*)  FROM=(--resume "${a#resume=}") ;;
    port=*)    PORT="${a#port=}" ;;
    workers=*) WORKERS="${a#workers=}" ;;
    *)         EXTRA+=("$a") ;;
  esac
done

# Free the canonical dashboard port so its URL is always the same one.
HOLD="$(ss -ltnp 2>/dev/null | grep -E ":${PORT}[[:space:]]" \
        | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"
if [ -n "${HOLD:-}" ]; then
  kill -9 "${HOLD}" 2>/dev/null || true
  sleep 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
LOG="${HERE}/runs-out/_launch_${CASE}_${DEPTH}_${TS}.log"
export SPEC_FLOW_DETACH_LOG="${LOG}"

OUT="$(bash "${HERE}/run-detached.sh" \
        --case "${CASE}" --depth "${DEPTH}" --workers "${WORKERS}" \
        --dashboard --dashboard-port "${PORT}" \
        "${FROM[@]}" "${EXTRA[@]}")"

PID="$(printf '%s\n' "${OUT}" | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"

# One compact status line: everything needed to watch or stop, no extra tokens.
echo "run: case=${CASE} depth=${DEPTH} workers=${WORKERS} v=next pid=${PID:-?}"
echo "dash: http://localhost:${PORT}"
echo "log:  ${LOG}"
echo "stop: kill ${PID:-<pid>}   # or: python3 ${HERE}/lib/run_cases.py --stop <run_dir>"
