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
# Use the project venv (dev-deps: gherkin-official, openapi-*-validator, jsonschema)
# if present, so the audit gate AND the run see the standard-oracle libraries.
# A bare system python3 lacks them -> the gherkin oracle test reds and every
# library-oracle degrades to a no-op mid-run (silent loss of spec validation).
PYBIN="${HERE}/../.venv/bin/python"
[ -x "${PYBIN}" ] || PYBIN="python3"
export PYBIN
TS="$(date +%Y%m%d-%H%M%S)"
LOG="${SPEC_FLOW_DETACH_LOG:-${HERE}/runs-out/_detached_${TS}.log}"
mkdir -p "$(dirname "${LOG}")"

# AUDIT GATE (ratchet step 3): a live run costs an hour; the offline self-audit
# costs seconds and reds on a design hole (missing branch, stub, class without a
# path). Never spend a run while the audit is red — fix the hole first. Set
# SPEC_FLOW_SKIP_AUDIT=1 only for a deliberate diagnostic run on a known-red audit.
if [ "${SPEC_FLOW_SKIP_AUDIT:-0}" != "1" ]; then
    echo "[run-detached] audit gate: pytest tests/audit …"
    if ! "${PYBIN}" -m pytest "${HERE}/audit" -q >/dev/null 2>&1; then
        echo "[run-detached] AUDIT RED — refusing to launch. Fix the design hole"\
             "(python3 -m pytest tests/audit) then re-run, or set"\
             "SPEC_FLOW_SKIP_AUDIT=1 for a deliberate diagnostic run." >&2
        exit 3
    fi
    echo "[run-detached] audit green — launching run."
fi

# A run that dies early under setsid+redirect used to leave a 0-byte log: with a
# file (not a tty) on stdout Python BLOCK-buffers, so a SIGKILL (e.g. a transient
# OOM, or session churn on /compact) discards the unflushed banner AND any
# traceback — the death becomes a ghost with zero evidence (lost v110/v111 this
# way). Force unbuffered output so the very first line and any crash hit disk
# immediately; a 0-byte log now means "never even started", a non-empty tail
# always carries the real cause.
#
# An eager heartbeat is written to the log BEFORE exec so the observer can tell a
# launched-but-killed run from one that never spawned.
# Checkpoint recording is ON for every detached run: a run that dies or must
# be replayed (--from-run / --from-checkpoint) needs snapshots to resume from.
# An explicit --checkpoint-every on the command line wins over this default.
case " $* " in
  *" --checkpoint-every"*) : ;;
  *) set -- "$@" --checkpoint-every 1 ;;
esac

echo "[run-detached] launching pid-of-setsid… ts=${TS} args=$* log=${LOG}" >"${LOG}"

# setsid => new session (no controlling terminal => immune to caller's SIGHUP)
# </dev/null => no stdin tie to the caller; >>LOG => own log (append, keep the
# heartbeat); & + disown => the caller returns immediately and owns nothing.
# PYTHONUNBUFFERED + python3 -u => line/stream flushes survive an abrupt kill.
# PYTHONFAULTHANDLER=1 => a C-level fault (segfault/abort) in ANY thread dumps
# every thread's stack to stderr (-> LOG) instead of dying silently; belt-and-
# suspenders with run_cases' own faulthandler.enable() (covers a pre-import crash).
# Supervisor shell: waits for python and RECORDS THE EXIT CODE in the log.
# v148 died with zero evidence — no traceback, no faulthandler dump, log stopped
# mid-run. That silence is only possible with SIGKILL (SIGTERM is registered in
# run_cases' faulthandler and would dump stacks). The supervisor makes every
# death attributable:
#   "exited rc=N"            -> engine returned / python raised (N<128)
#   "KILLED by signal S"     -> the python pid alone was killed (rc=128+S)
#   NO rc line in the log    -> the whole session group was swept (cgroup kill):
#                               the supervisor died with its child.
PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 setsid bash -c '
    "${PYBIN:-python3}" -u "$0" "$@"
    rc=$?
    ts="$(date "+%F %T")"
    if [ "$rc" -ge 128 ]; then
        echo "[run-detached] ${ts} python KILLED by signal $((rc-128)) (rc=${rc}) — external kill, not an engine exit"
    else
        echo "[run-detached] ${ts} python exited rc=${rc}"
    fi
' "${HERE}/lib/run_cases.py" "$@" \
    </dev/null >>"${LOG}" 2>&1 &
PID=$!
disown "${PID}" 2>/dev/null || true
echo "detached pid=${PID} log=${LOG}"
