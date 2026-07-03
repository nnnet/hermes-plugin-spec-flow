"""Deterministic diagnosers (Ф1): map a failed gate + its evidence to one or
more CAUSE findings, using only code-checkable signals (confidence 1.0). The
semantic causes (vague_spec, weak_implementer, task_check_mismatch,
goal_disconnect) are left to the LLM classifier (Ф4); here we cover the
structural ones, reusing the engine's existing detectors via injected helpers.

The registry is coarse->fine and additive, mirroring ``_dup_surface_findings``.
Each detector is ``fn(node, gate, verdict, evidence, context, helpers)`` and
returns ``list[Finding]`` (possibly empty). ``helpers`` is a small namespace the
runner binds (loops list, surface lookups) so this module never imports runner
internals.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from spec_flow_doctor import Finding
from spec_flow_remedies import semantic_causes

_LOG = logging.getLogger("spec_flow.doctor")


def _ev(evidence: Any) -> dict:
    """Normalise heterogeneous evidence into a dict the detectors read."""
    if isinstance(evidence, dict):
        return evidence
    if isinstance(evidence, (list, tuple)):
        return {"scope_findings": list(evidence)}
    return {"text": str(evidence or "")}


# --- structural detectors ----------------------------------------------------
def _gate_cause(gate: str) -> str:
    """S12.4 (v160): the fallback cause id DERIVES from the originating gate id
    ('test_status_gate' -> 'test_status') so a gate finding opens a cause NAMED
    BY ITS GATE — never shoved into an unrelated bucket. In v160 a test-status
    violation was filed as empty_delta ('delivered nothing' — files WERE
    delivered); the mislabeled cause never closed and flipped a green terminal."""
    g = str(gate or "")
    if g.endswith("_gate"):
        g = g[: -len("_gate")]
    return g or "empty_delta"


def _scope_findings(node, gate, verdict, ev, ctx, helpers) -> list:
    """A late-requirement spec that re-declares existing routes/symbols and adds
    no new surface = empty delta (the v041 failure). Scope findings come from the
    engine's _dup_surface_findings, passed in as evidence['scope_findings'].
    Findings whose text does not name a hollow/oversized delta open a cause
    derived from the ORIGINATING gate (S12.4), keeping the ledger attributable."""
    findings = ev.get("scope_findings") or []
    fallback = ("empty_delta" if "delta" in str(gate or "").lower()
                else _gate_cause(gate))
    out = []
    for f in findings:
        s = str(f)
        if "no new route" in s or "no new symbol" in s or "describe ONLY" in s:
            out.append(Finding(cause="empty_delta", detector="scope_findings",
                               evidence=s[:200]))
        elif "spans" in s or "scope-breadth" in s:
            out.append(Finding(cause="excess_input", detector="scope_findings",
                               evidence=s[:200]))
        else:
            out.append(Finding(cause=fallback, detector="scope_findings",
                               evidence=s[:200]))
    return out


def _leaf_metrics(node, gate, verdict, ev, ctx, helpers) -> list:
    """A node forced to BRANCH because it exceeds atomicity thresholds = too
    large. Evidence carries leaf_check reasons/over-threshold flags."""
    reasons = ev.get("leaf_reasons") or []
    if gate == "leaf_check" and (str(verdict).lower() == "branch" or reasons):
        big = any(k in " ".join(map(str, reasons))
                  for k in ("modules", "tasks", "loc", "interfaces"))
        if big or reasons:
            return [Finding(cause="task_too_large", detector="leaf_metrics",
                            evidence="; ".join(map(str, reasons))[:200] or "over thresholds")]
    return []


def _verdict_spread(node, gate, verdict, ev, ctx, helpers) -> list:
    """>=2 differing verdicts recorded on the SAME (node, gate) key = the checks
    disagree about one flaw."""
    loops = getattr(helpers, "loops", None) or []
    seen = set()
    for lp in loops:
        if lp.get("task") == node and str(lp.get("place", "")).endswith(gate):
            v = lp.get("verdict") or lp.get("outcome")
            if v:
                seen.add(str(v))
    if len(seen) >= 2:
        return [Finding(cause="inconsistent_checks", detector="verdict_spread",
                        evidence=f"verdicts on {node}:{gate} = {sorted(seen)}")]
    return []


def _rewrite_loops(node, gate, verdict, ev, ctx, helpers) -> list:
    """The same rejection reason across >=2 attempts = fix-by-rewrite repeating
    the error. Evidence carries prior reasons per attempt."""
    history = ev.get("reason_history") or []
    if len(history) >= 2 and history[-1] and history[-1] == history[-2]:
        return [Finding(cause="rewrite_loops", detector="rewrite_loops",
                        evidence=str(history[-1])[:200])]
    return []


def _hidden_deps(node, gate, verdict, ev, ctx, helpers) -> list:
    """The node depends on siblings that have not completed yet."""
    deps = list(getattr(ctx, "depends_on", ()) or [])
    done = set(getattr(helpers, "completed", None) or [])
    missing = [d for d in deps if d not in done]
    if missing:
        return [Finding(cause="hidden_deps", detector="hidden_deps",
                        evidence=f"unmet deps: {missing}")]
    return []


def _ancestry(node, gate, verdict, ev, ctx, helpers) -> list:
    """An ancestor/dependency already failed and its garbage propagated here."""
    loops = getattr(helpers, "loops", None) or []
    deps = set(getattr(ctx, "depends_on", ()) or [])
    for lp in loops:
        if lp.get("task") in deps and str(lp.get("type", "")).endswith("fail"):
            return [Finding(cause="garbage_accumulation", detector="ancestry",
                            evidence=f"upstream fail in {lp.get('task')}")]
    return []


def _memory_absent(node, gate, verdict, ev, ctx, helpers) -> list:
    """Prior decisions/constraints missing from the worker context. Needs a
    decisions snapshot the runner supplies as evidence['missing_decisions']."""
    miss = ev.get("missing_decisions") or []
    if miss:
        return [Finding(cause="context_loss", detector="memory_absent",
                        evidence=f"absent decisions: {list(miss)[:5]}")]
    return []


def _truncation(node, gate, verdict, ev, ctx, helpers) -> list:
    """Something was silently dropped. Needs dropped-segment markers the runner
    logs as evidence['dropped']."""
    dropped = ev.get("dropped") or []
    if dropped:
        return [Finding(cause="silent_truncation", detector="truncation_marks",
                        evidence=f"dropped: {list(dropped)[:5]}")]
    return []


# coarse -> fine; all matches reported (the Doctor ranks root->symptom)
DETECTORS = [
    ("hidden_deps", _hidden_deps),
    ("ancestry", _ancestry),
    ("memory_absent", _memory_absent),
    ("truncation_marks", _truncation),
    ("scope_findings", _scope_findings),
    ("leaf_metrics", _leaf_metrics),
    ("verdict_spread", _verdict_spread),
    ("rewrite_loops", _rewrite_loops),
]


class _Helpers:
    """Lightweight namespace the runner fills (loops/completed/...)."""
    def __init__(self, **kw):
        self.loops = kw.get("loops") or []
        self.completed = kw.get("completed") or []
        for k, v in kw.items():
            setattr(self, k, v)


# --- semantic classifier (Ф4) ------------------------------------------------
# Causes a deterministic detector cannot see; only the LLM evaluator decides.
# Loaded from spec_flow_doctor.defaults.yaml (causes whose detector is 'prompt')
# so it never drifts from the cause table — no literal list here.
import spec_flow_remedies as _remedies      # noqa: E402

SEMANTIC_CAUSES = _remedies.semantic_causes()


def _parse_vote(text: str) -> tuple:
    """Parse '<cause_id> | <cited evidence>' -> (cause|None, cite). 'abstain'
    (or any non-enum cause) yields (None, ...)."""
    s = str(text or "").strip()
    if not s:
        return (None, "")
    head, _, cite = s.partition("|")
    cause = head.strip().strip("`\"'").lower()
    cite = cite.strip()
    if cause == "abstain" or cause not in SEMANTIC_CAUSES:
        return (None, cite)
    return (cause, cite)


class Classifier:
    """LLM cause classifier for semantic gates. Reliability knobs come from the
    evaluator config: tier (via chain_for), temperature ~0, N votes + majority,
    mandatory citation, abstain->escalate. ``ask``/``chain_for`` are injected so
    this stays unit-testable with a fake backend."""

    def __init__(self, evaluator_cfg: dict, ask, chain_for):
        self.cfg = evaluator_cfg or {}
        self.ask = ask
        self.chain_for = chain_for

    def _prompt(self, gate, verdict, ev, ctx) -> str:
        reasons = ev.get("reasons") or ev.get("text") or ev.get("scope_findings") or ""
        if isinstance(reasons, (list, tuple)):
            reasons = "; ".join(map(str, reasons))
        return (
            f"A '{gate}' check returned {verdict}. Diagnose the single ROOT cause.\n"
            f"Evidence:\n{str(reasons)[:1500]}\n\n"
            f"Pick EXACTLY ONE cause id from: {', '.join(SEMANTIC_CAUSES)} — "
            "or 'abstain' if unsure. You MUST cite a concrete phrase copied from "
            "the evidence above as justification.\n"
            "Reply EXACTLY in one line: <cause_id> | <cited phrase>")

    def run(self, *, node, gate, verdict, evidence, context) -> dict:
        ev = _ev(evidence)
        prompt = self._prompt(gate, verdict, ev, context)
        tier = self.cfg.get("tier", "strong")
        try:
            chain = self.chain_for(tier) if self.chain_for else [""]
        except Exception:           # noqa: BLE001
            chain = [""]
        votes = max(1, int(self.cfg.get("votes", 1)))
        temp = float(self.cfg.get("temperature", 0.0))
        require_cite = bool(self.cfg.get("require_cite", True))
        _LOG.info("classify CALL node=%s gate=%s verdict=%s tier=%s model=%s votes=%d",
                  node, gate, verdict, self.cfg.get("tier"), chain[0], votes)
        tally: dict = {}
        cites: dict = {}
        raw: list = []
        for i in range(votes):
            try:
                reply = self.ask(prompt, model=chain[0], role="reviewer",
                                 step="diagnose", fallbacks=tuple(chain[1:]),
                                 params={"temperature": temp},
                                 meta={"node": node, "purpose": "diagnose"})
            except Exception as exc:        # noqa: BLE001
                _LOG.warning("classify vote %d/%d ERROR node=%s: %r",
                             i + 1, votes, node, exc)
                reply = ""
            _LOG.info("classify REPLY %d/%d node=%s: %s",
                      i + 1, votes, node, str(reply)[:160])
            raw.append(str(reply)[:200])
            cause, cite = _parse_vote(reply)
            if cause is None:
                continue
            if require_cite and not cite:
                _LOG.info("classify DISCARD node=%s cause=%s (no citation)",
                          node, cause)
                continue            # ungrounded -> discard before tallying
            tally[cause] = tally.get(cause, 0) + 1
            cites.setdefault(cause, cite)
        if not tally:
            _LOG.info("classify RESULT node=%s -> ABSTAIN (no grounded vote)", node)
            return {"findings": [], "abstained": True, "raw": raw}
        win = max(tally, key=tally.get)
        majority = int(self.cfg.get("majority", 1))
        if tally[win] < majority:
            _LOG.info("classify RESULT node=%s -> ABSTAIN (no majority, tally=%s)",
                      node, tally)
            return {"findings": [], "abstained": True, "raw": raw}
        _LOG.info("classify RESULT node=%s -> cause=%s votes=%d/%d tally=%s",
                  node, win, tally[win], votes, tally)
        conf = tally[win] / float(votes)
        f = Finding(cause=win, detector="prompt_clf",
                    evidence=cites.get(win, "")[:200], confidence=conf,
                    source="semantic")
        # log_vote_spread: surface the distribution for the "need more votes?" call
        spread = dict(tally) if self.cfg.get("log_vote_spread", True) else None
        return {"findings": [f], "abstained": False, "spread": spread, "raw": raw}


class Diagnosers:
    """Deterministic detector registry. ``helpers`` is bound by the runner."""

    def __init__(self, helpers: Any = None):
        self.helpers = helpers if helpers is not None else _Helpers()

    def run(self, *, node: str, gate: str, verdict: str,
            evidence: Any, context: Any) -> list:
        ev = _ev(evidence)
        out: list = []
        for _name, fn in DETECTORS:
            try:
                out.extend(fn(node, gate, verdict, ev, context, self.helpers) or [])
            except Exception:       # noqa: BLE001 — a broken detector must not
                continue            # crash diagnosis; just skip it
        # de-dup by cause, keep the highest-confidence finding per cause
        best: dict = {}
        for f in out:
            cur = best.get(f.cause)
            if cur is None or f.confidence > cur.confidence:
                best[f.cause] = f
        return list(best.values())
