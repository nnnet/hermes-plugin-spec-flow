"""Doctor: diagnose the CAUSE of a failure (with context) and dispatch a remedy
from the config table — replacing place-only hardcoded recovery.

This module is the stable spine: the data model, the PURE control-loop math
(ladder / oscillation / ranking / ceiling — no I/O, unit-tested offline) and the
``Doctor`` facade. The facade is pure-decision: ``diagnose`` runs injected
detectors and an optional LLM classifier; ``treat`` picks a remedy and returns an
``Action`` for the engine to EXECUTE. The Doctor never emits events or calls
workers itself — that stays in the runner (single emit writer, single worker
caller).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from spec_flow_remedies import (causes_config, doctor_config, escalation_kinds,
                                evaluator_config, levels_order)


# --- data model --------------------------------------------------------------
@dataclass(frozen=True)
class Context:
    # place
    node: str = ""
    module: str = ""
    gate: str = ""
    depth: int = 0
    # time
    attempt: int = 0
    version: int = 0
    changed_since_last: tuple = ()
    # connectedness
    depends_on: tuple = ()
    surface_neighbors: tuple = ()

    @property
    def place(self) -> str:
        """Counter key: a cause is tracked per (cause, place)."""
        return f"{self.node}:{self.gate}"


@dataclass(frozen=True)
class Finding:
    """A diagnosed cause WITH its grounding. Every cause must cite evidence."""
    cause: str
    detector: str
    evidence: str = ""
    confidence: float = 1.0
    source: str = "deterministic"   # deterministic | semantic | config


@dataclass(frozen=True)
class Diagnosis:
    context: Context
    ranked: tuple = ()              # tuple[Finding], root->symptom
    abstained: bool = False

    @property
    def primary(self) -> Optional[Finding]:
        return self.ranked[0] if self.ranked else None


@dataclass(frozen=True)
class Remedy:
    name: str
    params: dict = field(default_factory=dict)


@dataclass
class TreatmentRecord:
    """Appended to engine.loops as a plain dict (schema-compatible)."""
    node: str
    cause: str
    place: str
    remedy: str
    rung: int
    level: int = 0
    verdict_before: str = ""
    outcome: str = ""              # resolved | persisted | escalated | capped

    def as_loop(self) -> dict:
        return {"type": "doctor", "task": self.node, "cause": self.cause,
                "place": self.place, "remedy": self.remedy, "rung": self.rung,
                "level": self.level, "outcome": self.outcome,
                "detail": self.verdict_before}


@dataclass(frozen=True)
class Action:
    """What the runner executes. ``kind`` is the switch."""
    kind: str                      # rework|split|escalate|web_search|halt|record|
                                   # ask_human|noop|order_by_deps|patch_diff|...
    remedy: Remedy
    feedback: str = ""
    record: Optional[TreatmentRecord] = None


# --- per-node doctor state ----------------------------------------------------
def new_state() -> dict:
    return {"ladder": {}, "ring": [], "node_attempts": 0, "level": 0,
            "level_attempts": 0, "last_causeset": None, "nonshrink_rounds": 0,
            "open": {}}


# depth-level ladder leaf -> re-decompose parent -> re-spec -> human; loaded
# from spec_flow_doctor.defaults.yaml via the config loader (no literal here).
LEVELS = levels_order()
_ESCALATION_KINDS = escalation_kinds()


# --- PURE control-loop math (no I/O, unit-tested) ----------------------------
def ladder_for(cause_id: str, gate: str, causes_cfg: dict,
               default_ladder: list) -> list:
    """The remedy ladder for a cause, with a per-gate override applied."""
    spec = causes_cfg.get(cause_id) or {}
    by_gate = (spec.get("by_gate") or {}).get(gate) or {}
    return list(by_gate.get("ladder") or spec.get("ladder") or default_ladder)


def ladder_step(cause_id: str, place: str, state: dict,
                ladder: list) -> tuple:
    """Pick the next rung for (cause, place); never repeats a rung.

    Returns (remedy_name, rung_index) or ("escalate_level", -1) when the ladder
    for this (cause, place) is exhausted.
    """
    key = f"{cause_id}@{place}"
    n = int(state["ladder"].get(key, 0))
    if n >= len(ladder):
        return ("escalate_level", -1)
    state["ladder"][key] = n + 1
    return (ladder[n], n)


def push_ring(state: dict, cause_id: str, size: int) -> None:
    ring = state.setdefault("ring", [])
    ring.append(cause_id)
    del ring[:-size]                # keep only the last ``size`` entries


def is_oscillation(ring: list, max_period: Optional[int] = None,
                   repeats: int = 2) -> bool:
    """Detect a back-to-back repeating MULTI-cause cycle (A,B,A,B / A,B,C,A,B,C).

    Period-1 (the SAME cause again and again) is deliberately NOT oscillation —
    the ladder already escalates that case when its rungs run out. We only flag
    a genuine cycle of >=2 distinct causes, which signals a wrong split that must
    be fixed a level up rather than at the leaf.
    """
    n = len(ring)
    hi = max_period or (n // repeats)
    for p in range(2, hi + 1):
        seg = ring[-p * repeats:]
        if len(seg) < p * repeats:
            continue
        block = seg[:p]
        if len(set(block)) > 1 and all(
                seg[i] == block[i % p] for i in range(len(seg))):
            return True
    # short A-B-A oscillation (period 2 seen 1.5x)
    if n >= 3 and ring[-1] == ring[-3] != ring[-2]:
        return True
    return False


def rank_causes(findings: list, causes_order: list) -> list:
    """Sort findings root->symptom by causes_order, then by -confidence."""
    pos = {c: i for i, c in enumerate(causes_order)}
    return sorted(
        findings,
        key=lambda f: (pos.get(f.cause, len(causes_order)), -float(f.confidence)))


def causeset(findings: list) -> frozenset:
    return frozenset((f.cause for f in findings))


def set_shrinking(prev: Optional[frozenset], new: frozenset) -> bool:
    """True if the new cause-set is strictly smaller or genuinely changed
    (some prior cause removed). False => stuck."""
    if prev is None:
        return True
    if len(new) < len(prev):
        return True
    return bool(prev - new) and new != prev


def ceiling_reached(state: dict, levels: int, attempts_per_level: int) -> bool:
    return int(state.get("node_attempts", 0)) >= levels * attempts_per_level


def find_root(loops: list, node: str, depends_on=()) -> str:
    """Trace the root source of a failure: the EARLIEST failed dependency in the
    chronological loop journal, else the node itself. Used by the garbage_accum
    remedy so we treat the source, not the polluted downstream node."""
    deps = set(depends_on or [])
    for lp in (loops or []):              # loops are append-order = chronological
        t = str(lp.get("type", ""))
        if (t.endswith("fail") or t.endswith("reject")) and lp.get("task") in deps:
            return str(lp.get("task"))
    return node


def doctor_metrics(loops: list) -> dict:
    """Summarise the doctor's work from the loop journal for run reports and the
    compare tool: how many treatments, by cause/remedy/outcome, resolved ratio."""
    d = [lp for lp in (loops or []) if lp.get("type") == "doctor"]
    by_cause: dict = {}
    by_remedy: dict = {}
    by_outcome: dict = {}
    for lp in d:
        by_cause[lp.get("cause", "?")] = by_cause.get(lp.get("cause", "?"), 0) + 1
        by_remedy[lp.get("remedy", "?")] = by_remedy.get(lp.get("remedy", "?"), 0) + 1
        oc = lp.get("outcome") or "open"
        by_outcome[oc] = by_outcome.get(oc, 0) + 1
    resolved = by_outcome.get("resolved", 0)
    return {"treatments": len(d), "by_cause": by_cause, "by_remedy": by_remedy,
            "by_outcome": by_outcome,
            "resolved_ratio": (resolved / len(d)) if d else 0.0}


# --- Doctor facade -----------------------------------------------------------
class Doctor:
    """Diagnose + treat. Pure decision: returns Actions, executes nothing.

    ``diagnosers`` is the detector registry (Ф1) exposing
    ``run(node, gate, verdict, evidence, context) -> list[Finding]``.
    ``classifier`` (optional, Ф4) is the LLM semantic classifier with the same
    signature; it fires only when no deterministic finding matched and the gate
    is semantic-eligible.
    """

    def __init__(self, project: Optional[dict] = None,
                 overrides: Optional[dict] = None,
                 diagnosers: Any = None, classifier: Any = None):
        self.doctor = doctor_config(project, overrides)
        self.causes = causes_config(project, overrides)
        self.evaluator = evaluator_config(project, overrides)
        self.diagnosers = diagnosers
        self.classifier = classifier

    @property
    def enabled(self) -> bool:
        return bool(self.doctor.get("enabled"))

    def diagnose(self, *, node: str, gate: str, verdict: str,
                 evidence: Any, context: Context) -> Diagnosis:
        findings: list = []
        if self.diagnosers is not None:
            findings = list(self.diagnosers.run(
                node=node, gate=gate, verdict=verdict,
                evidence=evidence, context=context) or [])
        abstained = False
        if not findings and self.classifier is not None and \
                gate in (self.evaluator.get("semantic_gates") or []):
            sem = self.classifier.run(node=node, gate=gate, verdict=verdict,
                                      evidence=evidence, context=context)
            findings = list((sem or {}).get("findings") or [])
            abstained = bool((sem or {}).get("abstained"))
        ranked = rank_causes(findings, self.doctor.get("causes_order") or [])
        return Diagnosis(context=context, ranked=tuple(ranked),
                         abstained=abstained)

    def treat(self, diagnosis: Diagnosis, state: dict) -> Action:
        """Pick the next remedy for the primary cause and return an Action."""
        ctx = diagnosis.context
        state.setdefault("node_attempts", 0)
        state["node_attempts"] += 1

        if ceiling_reached(state, self.doctor["levels"],
                           self.doctor["attempts_per_level"]):
            return Action(kind="record", remedy=Remedy("record"),
                          feedback="treatment ceiling reached — recording debt",
                          record=TreatmentRecord(
                              node=ctx.node, cause="(ceiling)", place=ctx.place,
                              remedy="record", rung=-1, level=state.get("level", 0),
                              outcome="capped"))

        if diagnosis.abstained or diagnosis.primary is None:
            return self._escalate_or_human(ctx, state, reason="abstain")

        cause = diagnosis.primary.cause
        push_ring(state, cause, int(self.doctor["ring"]))
        if is_oscillation(state["ring"]):
            return self._escalate_level(ctx, state, reason="oscillation")

        ladder = ladder_for(cause, ctx.gate, self.causes,
                            self.doctor["default_ladder"])
        remedy_name, rung = ladder_step(cause, ctx.place, state, ladder)
        if remedy_name == "escalate_level":
            return self._escalate_level(ctx, state, reason="ladder-exhausted")

        rec = TreatmentRecord(node=ctx.node, cause=cause, place=ctx.place,
                              remedy=remedy_name, rung=rung,
                              level=state.get("level", 0),
                              verdict_before=str(diagnosis.primary.evidence)[:120])
        return Action(kind=remedy_name, remedy=Remedy(remedy_name),
                      feedback=str(diagnosis.primary.evidence), record=rec)

    # -- escalation helpers ---------------------------------------------------
    def _escalate_level(self, ctx: Context, state: dict, reason: str) -> Action:
        state["level"] = min(state.get("level", 0) + 1, self.doctor["levels"] - 1)
        state["ring"] = []          # leaf history irrelevant after a level change
        level_name = LEVELS[min(state["level"], len(LEVELS) - 1)]
        if level_name == "human":
            return self._human(ctx, state, reason=reason)
        kind = _ESCALATION_KINDS.get(level_name, "record")
        return Action(kind=kind, remedy=Remedy(kind),
                      feedback=f"escalate to level={level_name} ({reason})",
                      record=TreatmentRecord(
                          node=ctx.node, cause="(escalate)", place=ctx.place,
                          remedy=kind, rung=-1, level=state["level"],
                          outcome="escalated"))

    def _escalate_or_human(self, ctx: Context, state: dict, reason: str) -> Action:
        human = self.doctor.get("human") or {}
        if human.get("enable") and human.get("on_abstain"):
            return self._human(ctx, state, reason=reason)
        return self._escalate_level(ctx, state, reason=reason)

    def _human(self, ctx: Context, state: dict, reason: str) -> Action:
        human = self.doctor.get("human") or {}
        if not human.get("enable"):
            return Action(kind="record", remedy=Remedy("record"),
                          feedback=f"human disabled — record ({reason})")
        return Action(kind="ask_human", remedy=Remedy(
            "ask_human", {"timeout_s": human.get("timeout_s", 300),
                          "on_timeout": human.get("on_timeout", "record")}),
            feedback=f"escalate to human ({reason})",
            record=TreatmentRecord(node=ctx.node, cause="(human)", place=ctx.place,
                                   remedy="ask_human", rung=-1,
                                   level=state.get("level", 0), outcome="escalated"))
