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

import ast
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from spec_flow_remedies import (causes_config, counterexample_config,
                                doctor_config, escalation_kinds,
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


# --- counterexample-driven ONE-FUNCTION repair (node D1, Stage 19) ------------
# Why: every existing repair path re-asks the model with the WHOLE module plus
# the raw pytest dump, inviting a rewrite of everything it sees (the v159
# route-erasing rework class). Here the degrees of freedom match the defect:
# a failing ENGINE-COMPILED contract test (B3) is reduced by deterministic code
# to a COUNTEREXAMPLE (function, input, expected, got); the re-ask carries only
# that one function's slot (C1 def line + contract anchor) and the write door
# splices ONLY that function's body — a full-file rewrite is impossible by
# construction. Pure: no I/O here; the test run and the model call are injected
# (the same seam the Classifier uses). Test: tests/audit/
# test_counterexample_repair.py (S19.1-S19.5).

# the ONE contract sentence shared by the prompt and the door — prompt states,
# code guarantees (never let the two drift)
BODY_ONLY_RULE = ("Return ONLY the statements of this one function's body — "
                  "no def line, no module-level code, no other function, "
                  "no prose, no code fence.")

# pytest failure-section header: `____ test_name ____`
_SECTION_RE = re.compile(r"^_{3,}\s+(test_\w+)\s+_{3,}\s*$")
# the engine-compiled invoke step: `status, body = _invoke(handler, <args>)`
_INVOKE_RE = re.compile(r"_invoke\(\s*(\w+)\s*,\s*(.*)\)\s*$")


@dataclass(frozen=True)
class Counterexample:
    """One failing contract fact: which function, on what input, what the
    contract expected and what the run observed. Extracted by ENGINE CODE
    from the compiled-test failure output — never by an LLM."""
    test: str
    function: str
    method: str = ""
    path: str = ""
    payload: Any = None
    expected: str = ""
    got: str = ""


def _failure_sections(output: str) -> list:
    """Split pytest output into (test_name, section_lines) chunks.

    Why: the compiled-test failure section is the ONLY authoritative record
    of which statement failed and with what values.
    What: returns the FAILURES sections in output order; total — junk that
    carries no section header yields [].
    Test: garbage-output case in test_extraction_is_total_on_garbage_output.
    """
    sections: list = []
    name, buf = "", []
    for line in str(output or "").splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            if name:
                sections.append((name, buf))
            name, buf = m.group(1), []
            continue
        if name and line.startswith("="):        # summary separator ends all
            sections.append((name, buf))
            name, buf = "", []
            continue
        if name:
            buf.append(line)
    if name:
        sections.append((name, buf))
    return sections


def _section_counterexample(name: str, lines: list) -> Optional[Counterexample]:
    """One section -> one counterexample, or None when the section carries no
    engine-compiled invoke step (an import error, a fixture crash — those
    belong to other remedies, never guessed into a function blame).

    Why: attribution must be the FAILING step — pytest marks the failing
    statement with '>' and prints the source above it, so the LAST _invoke
    line at or before the marker is the step whose judgement failed (a
    scenario red on its given.state POST blames the POST handler, not the
    unreached when-step).
    What: parses handler/method/path/payload from the invoke line (literal by
    construction — the engine compiled it) and expected/got from the '>' and
    'E' lines.
    Test: test_scenario_failure_blames_the_failing_step.
    """
    fail_at = -1
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(">"):
            fail_at = i
    if fail_at < 0:
        return None
    invoke = None
    for ln in lines[:fail_at + 1]:
        m = _INVOKE_RE.search(ln)
        if m:
            invoke = m
    if invoke is None:
        return None
    try:
        args = ast.literal_eval("(%s)" % invoke.group(2))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(args, tuple) or len(args) < 3:
        return None
    expected = lines[fail_at].lstrip().lstrip(">").strip()
    got = "; ".join(ln[1:].strip() for ln in lines[fail_at:]
                    if ln.startswith("E ") or ln.rstrip() == "E").strip()
    return Counterexample(
        test=name, function=invoke.group(1), method=str(args[0]),
        path=str(args[1]), payload=args[2], expected=expected, got=got)


def extract_counterexamples(test_source: str, output: str) -> list:
    """Deterministic counterexamples from a compiled-test run's output.

    Why: the repair must know WHICH function failed on WHAT input without
    asking a model — the engine authored the test file, so it owns the
    grammar of both the source and the failure output.
    What: one Counterexample per pytest failure section whose test exists in
    the compiled source and whose failing step is an engine _invoke call;
    green runs, junk output and non-invoke failures yield [] (total).
    Test: tests/audit/test_counterexample_repair.py (S19.1).
    """
    src = str(test_source or "")
    out: list = []
    for name, lines in _failure_sections(output):
        if ("def %s(" % name) not in src:
            continue                     # not a compiled test of this leaf
        cx = _section_counterexample(name, lines)
        if cx is not None:
            out.append(cx)
    return out


def _module_fdef(module_src: str, function: str) -> tuple:
    """(ast tree, module-level FunctionDef) or ValueError naming the offence.

    Why: both the slot and the door need the SAME engine-owned lookup so they
    can never disagree on where the function lives.
    What: parses the module and returns its top-level def of `function`.
    Test: unknown-function refusal in test_door_refuses_unknown_function.
    """
    try:
        tree = ast.parse(module_src or "")
    except SyntaxError as exc:
        raise ValueError("module does not parse: %s" % exc)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return tree, node
    raise ValueError("function '%s' is not defined at module level — "
                     "the door has no slot for it" % function)


def function_slot(module_src: str, function: str) -> str:
    """The ONE function's engine slot: def line + contract anchor comments,
    WITHOUT the current body.

    Why: fresh context — replaying the failed body invites the model to
    patch around it; the slot plus the counterexample is the whole task.
    What: the source lines from the def keyword to just before the first
    body statement (the C1 anchor comment lives in that span).
    Test: test_function_slot_is_signature_and_anchor_only.
    """
    _tree, fdef = _module_fdef(module_src, function)
    lines = (module_src or "").splitlines()
    header = lines[fdef.lineno - 1:fdef.body[0].lineno - 1]
    if not header:                       # one-line def: keep the def line
        header = [lines[fdef.lineno - 1]]
    return "\n".join(header)


def repair_prompt(cx: Counterexample, slot: str) -> str:
    """The one-function re-ask with FRESH context.

    Why: minimum degrees of freedom — the prompt receives ONLY the slot and
    the counterexample, so a whole-file rewrite cannot even be requested.
    What: a compact directive: slot, input, expected, got, body-only rule.
    Test: test_repair_prompt_carries_one_function_and_no_module.
    """
    return (
        "You repair EXACTLY ONE function of a spec-driven product module.\n\n"
        "Function slot (engine-owned — the signature cannot change):\n"
        "%s\n\n"
        "Counterexample from the compiled contract-test run:\n"
        "  test:     %s\n"
        "  input:    %s %s payload=%r\n"
        "  expected: %s\n"
        "  got:      %s\n\n"
        "%s The write door splices your reply into this single body slot; "
        "everything else in the module is engine-frozen."
        % (slot, cx.test, cx.method, cx.path, cx.payload, cx.expected,
           cx.got, BODY_ONLY_RULE))


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.S)


def _reply_body(reply: str, function: str, want_args: list) -> str:
    """Normalise a model reply into a bare function BODY, or refuse.

    Why: the door's whole guarantee is that nothing but ONE body can land;
    every module-shaped reply must die HERE with a named refusal.
    What: strips a code fence; a reply that parses as a module is accepted
    only when it is EXACTLY one def of `function` with the engine argument
    list (its body is taken) or plain statements (taken verbatim); imports /
    sibling defs / renamed defs / rewritten argument lists are refused. A
    reply that only parses as a body (carries `return`) is syntax-probed.
    Test: test_door_refuses_module_shaped_reply,
    test_door_green_splice_keeps_engine_surface.
    """
    text = str(reply or "")
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    body = textwrap.dedent(text).strip("\n")
    if not body.strip():
        raise ValueError("empty repair reply — nothing to splice")
    module = None
    try:
        module = ast.parse(body)
    except SyntaxError:
        pass                             # body-only replies carry `return`
    if module is not None:
        stmts = module.body
        if len(stmts) == 1 and isinstance(stmts[0], ast.FunctionDef):
            fd = stmts[0]
            if fd.name != function:
                raise ValueError(
                    "reply defines '%s', the slot is '%s' — the door accepts "
                    "ONLY that function's body" % (fd.name, function))
            if [a.arg for a in fd.args.args] != list(want_args):
                raise ValueError(
                    "reply rewrites the argument list of '%s' — the "
                    "signature is engine-owned" % function)
            seg = body.splitlines()[fd.body[0].lineno - 1:fd.end_lineno]
            body = textwrap.dedent("\n".join(seg)).strip("\n")
        elif any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef, ast.Import, ast.ImportFrom))
                 for s in stmts):
            raise ValueError(
                "reply is module-shaped (imports / defs beyond the one "
                "'%s' slot) — full-file rewrites are refused by "
                "construction" % function)
    probe = ("def _probe(%s):\n" % ", ".join(want_args)
             + textwrap.indent(body, "    "))
    try:
        ast.parse(probe)
    except SyntaxError as exc:
        raise ValueError("reply is not a valid function body: %s" % exc)
    return body


def apply_function_body(module_src: str, function: str, reply: str,
                        allowed_effects: Any = ()) -> str:
    """The WRITE DOOR: splice a reply into EXACTLY ONE function's body.

    Why: acceptance of node D1 — a full-file rewrite is impossible BY
    CONSTRUCTION because only the named function's body region is ever
    replaced; the def line and the C1 anchor comments are engine-kept and
    every byte outside the block is carried over verbatim. H6/S17.5: a repair
    body is also under the closed EFFECT world — `open(path,'w')` needs no
    import, so the module-shape refusal never fired and the repair door was a
    second silent F2 hole.
    What: returns the new module text; ValueError (named) for an unknown
    function, a reply that is not one plain body, or a body performing an
    observable side effect not declared in `allowed_effects`.
    Test: tests/audit/test_counterexample_repair.py (S19.3),
    tests/audit/test_body_effect_door.py (S17.5).
    """
    _tree, fdef = _module_fdef(module_src, function)
    want_args = [a.arg for a in fdef.args.args]
    body = _reply_body(reply, function, want_args)
    # H6/S17.5: the same effect door a skeleton delivery passes through.
    from spec_skeletons import body_effect_findings
    eff = body_effect_findings(ast.parse(body), function,
                               {str(e) for e in (allowed_effects or ())})
    if eff:
        raise ValueError("repair body performs an undeclared side effect — "
                         "refused: %s" % "; ".join(eff))
    lines = (module_src or "").splitlines()
    header = lines[fdef.lineno - 1:fdef.body[0].lineno - 1]
    if not header:
        raise ValueError(
            "function '%s' is a one-line def — the engine skeleton owns the "
            "slot shape and never emits one" % function)
    out_lines = (lines[:fdef.lineno - 1] + header
                 + textwrap.indent(body, "    ").splitlines()
                 + lines[fdef.end_lineno:])
    out = "\n".join(out_lines)
    if (module_src or "").endswith("\n"):
        out += "\n"
    try:
        new_tree, new_fdef = _module_fdef(out, function)
    except ValueError as exc:
        raise ValueError("splice broke the module — refused: %s" % exc)
    if [a.arg for a in new_fdef.args.args] != want_args:
        raise ValueError("splice would change the signature of '%s' — "
                         "refused" % function)
    return out


def counterexample_repair(module_src: str, test_source: str, *,
                          run_tests: Callable, ask: Callable,
                          max_rounds: Optional[int] = None) -> dict:
    """The Ralph loop: fresh context, ONE task per iteration, objective exit.

    Why: a repair loop is honest only when its exit condition is the REAL
    contract-test run — a cooperative model that never fixes the defect must
    end red; and one counterexample per iteration keeps every re-ask small
    enough for a weak model (model-independence).
    What: run tests -> green? exit; else extract counterexamples, take the
    FIRST, re-ask that one function, splice through the door, repeat up to
    ``max_rounds`` (config datum). Returns {green, rounds, module, records};
    a red run with no extractable counterexample stops WITHOUT an LLM call.
    ``run_tests(module_src) -> (passed, output)`` and ``ask(prompt) -> reply``
    are injected (the Classifier's seam pattern) — this function does no I/O.
    Test: tests/audit/test_counterexample_repair.py (S19.4).
    """
    rounds = int(max_rounds if max_rounds is not None
                 else counterexample_config().get("max_rounds", 3))
    src = module_src
    records: list = []
    for rnd in range(1, max(1, rounds) + 1):
        passed, output = run_tests(src)
        if passed:
            return {"green": True, "rounds": rnd - 1, "module": src,
                    "records": records}
        cxs = extract_counterexamples(test_source, output)
        if not cxs:
            records.append({"round": rnd, "applied": False,
                            "detail": "no counterexample extractable from "
                                      "the failing run — stopping honestly"})
            return {"green": False, "rounds": rnd - 1, "module": src,
                    "records": records}
        cx = cxs[0]                      # ONE task per iteration
        rec = {"round": rnd, "test": cx.test, "function": cx.function,
               "applied": False}
        try:
            slot = function_slot(src, cx.function)
            reply = ask(repair_prompt(cx, slot))
            src = apply_function_body(src, cx.function, reply)
            rec["applied"] = True
        except ValueError as exc:
            rec["detail"] = str(exc)[:200]
        records.append(rec)
    passed, _output = run_tests(src)     # the objective FINAL verdict
    return {"green": bool(passed), "rounds": max(1, rounds), "module": src,
            "records": records}


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
