"""Declarative workflow driver for the implementer orchestra (phase 2).

Why: the orchestra used to run its specialists in a fixed linear order baked
into the loop. To support evaluator-optimizer loops (tester -> fixer -> tester)
and branches without hand-rolling control flow per case, the step order is now
described declaratively — either the `sequential` shorthand (today's behaviour)
or a small graph of `{from, to, when}` edges — and THIS module turns that spec
into the next role to run.

What: `WorkflowPlan.from_team(...)` parses a team's `process`/`workflow` spec;
`plan.run(state)` is a generator that yields the next role to execute. The
caller feeds back the last step's pass/fail through a mutable `state` object so
conditional edges (`when: tests_passed` / `tests_failed` / `retry`) resolve.

Test: pure stdlib, no LLM, no I/O — fully unit-tested in
tests/workers/test_orchestra_workflow.py (sequential order; a tester<->fixer
loop that exits on green; the max-iterations guard; unknown `when` = never).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# ── condition vocabulary ────────────────────────────────────────────────
# An edge's `when` decides whether it may be taken given the last step's
# pass/fail outcome. Anything outside this set is treated as NEVER (a typo must
# never silently behave like `always`).
WHEN_ALWAYS = "always"          # unconditional edge (the default)
WHEN_TESTS_PASSED = "tests_passed"   # last step left the leaf GREEN
WHEN_TESTS_FAILED = "tests_failed"   # last step left the leaf RED
WHEN_RETRY = "retry"            # loop-back after a repair step (alias of failed-context)

# DONE is the reserved sink: an edge `to: DONE` ends the run.
DONE = "DONE"

_PROCESS_SEQUENTIAL = "sequential"


@dataclass
class WorkflowState:
    """Mutable hand-off between the driver and the caller.

    Why: conditional edges need to know the OUTCOME of the role just executed;
    the caller writes it here after each yielded role.
    What: `passed` = did the last step leave the leaf green; `wrote` = did it
    produce any files (used by `retry` to distinguish a real repair attempt).
    Test: set passed=False then read it back inside run()'s edge resolution.
    """
    passed: bool = False
    wrote: bool = False
    last_role: str = ""


def _edge_matches(when: str, state: WorkflowState) -> bool:
    """Whether an edge with this `when` fires given the current state.

    Why: centralises the condition vocabulary so the graph walk stays tiny and
    an unknown `when` is uniformly rejected (never == typo-safe).
    What: maps each known `when` onto the state's pass/fail; unknown -> False.
    Test: tests_passed true only when state.passed; unknown returns False.
    """
    if when == WHEN_ALWAYS:
        return True
    if when == WHEN_TESTS_PASSED:
        return state.passed
    if when == WHEN_TESTS_FAILED:
        return not state.passed
    if when == WHEN_RETRY:
        # `retry` is the fixer's loop-back: it fires after a (failing) step so
        # the tester re-runs. We treat it as "the leaf is still not green".
        return not state.passed
    return False        # unknown `when` -> never


@dataclass
class WorkflowPlan:
    """A parsed orchestra workflow: the ordered roles plus an optional edge
    graph that drives non-linear (looping/branching) execution.

    Why: decouples "what order do specialists run in" from the orchestra loop so
    the loop just asks for the next role and executes its branch.
    What: `roles` is the specialists' role list (declaration order). When
    `graph` is empty the plan is sequential; otherwise the walk follows edges
    from `start`, resolving each `when` against the fed-back state, until it
    reaches DONE, runs out of matching edges, or hits the iteration guard.
    Test: from_team on a bare list yields sequential roles; from_team with a
    `workflow` graph loops tester<->fixer then exits on tests_passed.
    """
    roles: list[str]
    graph: dict[str, list[dict]] = field(default_factory=dict)
    start: str = ""
    max_iterations: int = 0

    # ── construction ────────────────────────────────────────────────────
    @classmethod
    def from_team(cls, specialists: Any, *, workflow: Optional[dict] = None,
                  process: Optional[str] = None) -> "WorkflowPlan":
        """Build a plan from a team's specialists and its (optional) flow spec.

        Why: one entry point that accepts today's shapes — a bare specialist
        list (sequential) — and the new `workflow:` graph, defaulting to
        sequential so back-compat is exact.
        What: extracts role names in declaration order; if `workflow` carries
        `edges`, builds an adjacency map and uses its `start` (else the first
        role). `process` other than sequential is currently treated as
        sequential (forward-compatible: parallel etc. land later).
        Test: a list of {role} dicts with no workflow -> sequential; a workflow
        with edges -> graph populated and start set.
        """
        roles = _roles_of(specialists)
        # The iteration guard scales with team size so a healthy loop has room
        # but a runaway tester<->fixer cycle is bounded.
        guard = max(1, len(roles) * 3)

        wf = workflow if isinstance(workflow, dict) else None
        edges = wf.get("edges") if wf else None
        if not isinstance(edges, list) or not edges:
            # No graph: sequential (the `process` shorthand). Any non-sequential
            # process value still degrades to sequential for now.
            return cls(roles=roles, graph={}, start="", max_iterations=guard)

        graph: dict[str, list[dict]] = {}
        for raw in edges:
            if not isinstance(raw, dict):
                continue
            src = str(raw.get("from", ""))
            dst = str(raw.get("to", ""))
            if not src or not dst:
                continue
            when = str(raw.get("when") or WHEN_ALWAYS)
            graph.setdefault(src, []).append({"to": dst, "when": when})

        start = str((wf.get("start") if wf else "") or "")
        if not start:
            start = roles[0] if roles else ""
        return cls(roles=roles, graph=graph, start=start, max_iterations=guard)

    # ── driving ─────────────────────────────────────────────────────────
    @property
    def is_sequential(self) -> bool:
        """True when no edge graph is configured (today's linear flow)."""
        return not self.graph

    def run(self, state: WorkflowState) -> Iterator[str]:
        """Yield roles to execute, one at a time, until the flow ends.

        Why: the caller runs each yielded role's branch then writes the outcome
        back into `state`; on the next iteration the driver reads that outcome
        to pick the next role — so loops/branches resolve without the caller
        knowing the topology.
        What: sequential mode yields `roles` in order once. Graph mode walks
        edges from `start`: after each yield it picks the FIRST edge whose
        `when` matches the (just-updated) state; a `to: DONE` (or no matching
        edge) ends the walk; the iteration guard caps total yields so a
        conditional loop cannot run forever.
        Test: sequential yields the declared order exactly once; a tester/fixer
        loop yields tester, fixer, tester, ... then stops at tests_passed; the
        guard fires when a loop never resolves.
        """
        if self.is_sequential:
            for role in self.roles:
                state.last_role = role
                yield role
            return

        current = self.start
        seen = 0
        while current and current != DONE:
            if seen >= self.max_iterations:
                # Guard: a never-resolving loop (e.g. tester<->fixer that never
                # goes green) is bounded rather than spinning forever.
                return
            seen += 1
            state.last_role = current
            yield current
            current = self._next(current, state)

    def _next(self, node: str, state: WorkflowState) -> str:
        """The next node after `node`, or DONE when nothing matches.

        Why: a deterministic edge picker keeps the walk total and predictable.
        What: returns the target of the first outgoing edge whose `when` fires
        against `state`; DONE when there is no such edge.
        Test: with state.passed=False, tester routes to its tests_failed edge;
        with passed=True it routes to its tests_passed (DONE) edge.
        """
        for edge in self.graph.get(node, []):
            if _edge_matches(str(edge.get("when") or WHEN_ALWAYS), state):
                return str(edge.get("to") or DONE)
        return DONE


def team_config_raw(llm_backend_mod: Any, environ: Any) -> Any:
    """Return the RAW implementer team config (dict or bare list), or None.

    Why: the orchestra loop is handed the already-normalised specialist LIST,
    which has dropped the `workflow:`/`process:` keys. To drive non-linear flow
    the loop needs the original team object; this re-sources it the same way the
    specialist parser does (env JSON overrides the WORKERS_CFG), READ-ONLY.
    What: parses SPEC_FLOW_IMPLEMENTER_TEAM (JSON) if set, else reads
    `llm_backend.WORKERS_CFG['implementer']['team']`. Returns whatever shape is
    found (dict for the canonical form, list for the legacy alias) or None.
    Test: with the env JSON set to a dict-with-workflow, returns that dict;
    unset + a WORKERS_CFG dict returns the cfg's team.
    """
    import json as _json
    env = environ.get("SPEC_FLOW_IMPLEMENTER_TEAM")
    if env is not None and str(env).strip():
        try:
            return _json.loads(env)
        except (ValueError, TypeError):
            return None
    cfg = getattr(llm_backend_mod, "WORKERS_CFG", None) or {}
    if not isinstance(cfg, dict):
        return None
    return (cfg.get("implementer") or {}).get("team")


def workflow_of(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """Extract the (workflow, process) flow spec from a raw team config.

    Why: the orchestra loop already normalises the specialist LIST elsewhere,
    but the FLOW spec lives on the canonical `team: {specialists, workflow,
    process}` dict; this pulls it out without re-parsing the specialists so the
    loop can build a WorkflowPlan.
    What: for a dict it returns (raw['workflow'] if a dict, raw['process'] if a
    str); for the legacy bare-list shape it returns (None, None) -> sequential.
    Test: a dict with a `workflow.edges` returns that dict; a bare list returns
    (None, None).
    """
    if isinstance(raw, dict):
        wf = raw.get("workflow")
        wf = wf if isinstance(wf, dict) else None
        proc = raw.get("process")
        proc = str(proc) if isinstance(proc, str) else None
        return wf, proc
    return None, None


def _roles_of(specialists: Any) -> list[str]:
    """Extract role names in declaration order from a specialists list.

    Why: the plan is keyed on roles; specialists may be dicts ({role: ...}) or
    already-bare role strings, so normalise both.
    What: returns [str(role), ...] skipping entries without a usable role.
    Test: a mix of {role} dicts and strings yields the role strings in order.
    """
    out: list[str] = []
    if not isinstance(specialists, list):
        return out
    for item in specialists:
        if isinstance(item, dict):
            role = item.get("role")
            if role:
                out.append(str(role))
        elif isinstance(item, str) and item:
            out.append(item)
    return out
