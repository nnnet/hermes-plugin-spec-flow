#!/usr/bin/env bash
# Launch a spec-flow case run FULLY DETACHED from any shell or session.
#
# HARD RULE: a run must NEVER depend on the caller (a Claude session, a tmux
# pane, an ssh connection). setsid puts the run in its own session and process
# group, so when the caller's shell dies (e.g. on /compact) no SIGHUP reaches
# the run. stdout+stderr go to a log file and stdin is detached; the observer
# only ever reads the log, never the live process.
#
# Usage: run-detached.sh <args passed verbatim to lib/run_cases.py>
# Prints: detached pid=<PID> log=<LOG>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="${SPEC_FLOW_DETACH_LOG:-${HERE}/runs-out/_detached_${TS}.log}"
mkdir -p "$(dirname "${LOG}")"

# setsid => new session (no controlling terminal => immune to caller's SIGHUP)
# </dev/null => no stdin tie to the caller; &>LOG => own log; & + disown => the
# caller returns immediately and owns nothing.
setsid python3 "${HERE}/lib/run_cases.py" "$@" </dev/null >"${LOG}" 2>&1 &
PID=$!
disown "${PID}" 2>/dev/null || true
echo "detached pid=${PID} log=${LOG}"
