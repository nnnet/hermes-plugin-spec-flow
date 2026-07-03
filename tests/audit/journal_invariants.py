"""Layer-1 audit: invariants over the run journal (trace.jsonl).

The journal is a stream of events with fields: t, tick, task, phase, action,
detail, gate, verdict, level. Rules (each violation becomes a Finding):

1. terminality — the LAST product-phase event carries verdict READY or
   NOT READY; a run with no terminal product verdict is broken.
2. full replacement — an event saying the base was "collapsed to one module"
   must be followed by a spec rewrite/cleanup event for that collapse.
   The v150 engine emits no such event, so this rule honestly goes red
   there (a KNOWN engine gap, kept red on purpose).
3. gated completion — every non-branch node that reaches ``to_done`` must
   earlier pass the card gate (a "card gate"/"card completeness" event) or
   an explicit review-tiering skip.
4. late requirement ownership — every "late requirement materialized/routed"
   event must be followed by events for that node (someone owns it) AND the
   node must close: reach ``to_done`` or an honest FAIL/rejected verdict.
   (The artifact half — the contribution actually being WIRED into the
   assembled entry — is consistency.check_module_routes_reach_entry.)
5. failure closure — every milestone-level FAIL (level == 1) must later be
   answered by a remedy/rework/reconcile/repair event for the same node OR
   the run must terminate NOT READY. A FAIL must never hang silently on a
   run that claims READY.

Library + CLI:  python3 tests/audit/journal_invariants.py <run_dir | trace.jsonl>
Exit code 1 when findings exist.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TERMINAL_VERDICTS = {"READY", "NOT READY"}

_COLLAPSE_MARKER = re.compile(r"collapsed to one module", re.IGNORECASE)
# Markers a spec-rewrite/cleanup event would carry after a collapse.
_RESPEC_MARKER = re.compile(
    r"rewrit|re-?spec|spec\s+(rewrite|rewritten|updated after collapse|pruned|cleanup)"
    r"|scope pruned|prune[sd]? collapsed",
    re.IGNORECASE,
)
_CARD_GATE_MARKER = re.compile(r"card (gate|completeness)", re.IGNORECASE)
_TIERING_SKIP_MARKER = re.compile(r"review tiering.*skip", re.IGNORECASE)
_LATE_REQ_MARKER = re.compile(r"late requirement", re.IGNORECASE)
_REMEDY_MARKER = re.compile(r"remedy|rework|reconcile|repair", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One violated journal invariant."""

    file: str  # journal path relative to the run dir
    kind: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.kind}] {self.file}: {self.message}"


def load_events(trace_path: Path) -> list[dict]:
    """Parse trace.jsonl into an ordered event list (blank lines skipped)."""
    events: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _text(event: dict) -> str:
    """action + detail as one searchable string."""
    return f"{event.get('action', '')} {event.get('detail', '')}"


def _base_task(event: dict) -> str:
    """'core:review' -> 'core' (node id without the sub-task suffix)."""
    return str(event.get("task", "")).split(":", 1)[0]


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def check_terminality(events: list[dict], journal: str) -> list[Finding]:
    """The last product-phase event must carry READY or NOT READY."""
    product_events = [e for e in events if e.get("phase") == "product"]
    if not product_events:
        return [
            Finding(
                file=journal,
                kind="no_terminal_product_event",
                message="journal has no product-phase event — the run never concluded",
            )
        ]
    last = product_events[-1]
    if last.get("verdict") not in TERMINAL_VERDICTS:
        return [
            Finding(
                file=journal,
                kind="non_terminal_product_verdict",
                message=(
                    f"last product event verdict is {last.get('verdict')!r}; "
                    f"expected one of {sorted(TERMINAL_VERDICTS)}"
                ),
            )
        ]
    return []


def check_collapse_respec(events: list[dict], journal: str) -> list[Finding]:
    """A 'collapsed to one module' event demands a later spec rewrite/cleanup.

    KNOWN v150 GAP: the engine does not emit the rewrite event yet, so this
    rule goes honestly red on such runs.
    """
    findings: list[Finding] = []
    for index, event in enumerate(events):
        if not _COLLAPSE_MARKER.search(_text(event)):
            continue
        rewritten = any(_RESPEC_MARKER.search(_text(later)) for later in events[index + 1:])
        if not rewritten:
            findings.append(
                Finding(
                    file=journal,
                    kind="collapse_without_spec_rewrite",
                    message=(
                        f"event #{index} (task={event.get('task')}) collapsed the base "
                        "to one module but no later event rewrites/cleans the collapsed "
                        "leaf spec (known v150 engine gap)"
                    ),
                )
            )
    return findings


def check_done_after_card_gate(events: list[dict], journal: str) -> list[Finding]:
    """Every non-branch node reaching to_done passed the card gate or a tiering skip."""
    branch_nodes = {
        _base_task(e) for e in events if str(e.get("verdict", "")).lower() == "branch"
    }
    findings: list[Finding] = []
    for index, event in enumerate(events):
        if event.get("action") != "to_done":
            continue
        node = _base_task(event)
        if node in branch_nodes:
            continue  # branches integrate children; the card gate is a leaf gate
        gated = any(
            _base_task(earlier) == node
            and (
                _CARD_GATE_MARKER.search(_text(earlier))
                or _TIERING_SKIP_MARKER.search(_text(earlier))
            )
            for earlier in events[:index]
        )
        if not gated:
            findings.append(
                Finding(
                    file=journal,
                    kind="done_without_card_gate",
                    message=(
                        f"event #{index}: node {node!r} reached to_done without a prior "
                        "card gate/completeness event or review-tiering skip"
                    ),
                )
            )
    return findings


def check_late_requirement_ownership(events: list[dict], journal: str) -> list[Finding]:
    """Every late requirement must reach a CLOSED state, not merely be touched.

    Two-level rule: (a) some later event must pick the node up at all
    (an owner exists); (b) the node must then either complete (``to_done``)
    or go honestly red (a FAIL/rejected verdict on the node). A late request
    that is touched but never closes either way silently evaporates — the
    journal-level half of «every late request reaches its contribution to
    the assembly or yields an honest red node» (the artifact-level half —
    the contribution actually being WIRED — lives in
    consistency.check_module_routes_reach_entry).
    """
    findings: list[Finding] = []
    for index, event in enumerate(events):
        if not _LATE_REQ_MARKER.search(str(event.get("action", ""))):
            continue
        node = _base_task(event)
        owned = closed = False
        for later in events[index + 1:]:
            if _base_task(later) != node:
                continue
            owned = True
            if later.get("action") == "to_done":
                closed = True
                break
            if str(later.get("verdict", "")).strip().upper() in (
                    "FAIL", "REJECTED"):
                closed = True  # an honest red node also closes the request
                break
        if not owned:
            findings.append(
                Finding(
                    file=journal,
                    kind="late_requirement_unowned",
                    message=(
                        f"event #{index}: late requirement materialized for node {node!r} "
                        "but no later event ever picks it up"
                    ),
                )
            )
        elif not closed:
            findings.append(
                Finding(
                    file=journal,
                    kind="late_requirement_unclosed",
                    message=(
                        f"event #{index}: late requirement for node {node!r} was "
                        "picked up but never reached to_done nor an honest "
                        "FAIL/rejected verdict"
                    ),
                )
            )
    return findings


def _gate_prefix(event: dict) -> str:
    """The gate identity of an event's action: text before ':' (or the whole
    action), lower-cased — 'card gate: incomplete …' -> 'card gate'."""
    return str(event.get("action", "")).split(":", 1)[0].strip().lower()


def check_fail_closure(events: list[dict], journal: str) -> list[Finding]:
    """A milestone FAIL must be remedied later OR the run must end NOT READY.

    Resolution is an EXPLICIT event, never the mere absence of a later
    failure (S10.15): either a remedy/rework/reconcile/repair event for the
    same node, or a later milestone PASS of the SAME gate prefix on the SAME
    node (the engine's 'card gate: satisfied after card fill' shape).
    """
    product_events = [e for e in events if e.get("phase") == "product"]
    terminal = product_events[-1].get("verdict") if product_events else None
    if terminal == "NOT READY":
        return []  # an honest red terminal closes every open FAIL
    findings: list[Finding] = []
    for index, event in enumerate(events):
        if event.get("level") != 1 or event.get("verdict") != "FAIL":
            continue
        node = _base_task(event)
        prefix = _gate_prefix(event)
        remedied = any(
            _base_task(later) == node
            and (
                _REMEDY_MARKER.search(_text(later))
                or (
                    str(later.get("verdict", "")).upper() == "PASS"
                    and later.get("level") == 1
                    and prefix
                    and _gate_prefix(later) == prefix
                )
            )
            for later in events[index + 1:]
        )
        if not remedied:
            findings.append(
                Finding(
                    file=journal,
                    kind="unresolved_milestone_fail",
                    message=(
                        f"event #{index} (task={event.get('task')}, "
                        f"action={event.get('action')!r}) FAILed at milestone level with "
                        "no later remedy/rework/reconcile and no NOT READY terminal"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def audit(target: Path) -> list[Finding]:
    """Run all journal invariants; accepts a run dir or a trace.jsonl path."""
    target = Path(target)
    trace_path = target / "trace.jsonl" if target.is_dir() else target
    journal = trace_path.name
    if not trace_path.is_file():
        return [
            Finding(file=journal, kind="missing_artifact", message=f"{trace_path} not found")
        ]
    events = load_events(trace_path)

    findings: list[Finding] = []
    findings.extend(check_terminality(events, journal))
    findings.extend(check_collapse_respec(events, journal))
    findings.extend(check_done_after_card_gate(events, journal))
    findings.extend(check_late_requirement_ownership(events, journal))
    findings.extend(check_fail_closure(events, journal))
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python3 tests/audit/journal_invariants.py <run_dir | trace.jsonl>",
            file=sys.stderr,
        )
        return 2
    findings = audit(Path(argv[1]))
    for finding in findings:
        print(finding)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
