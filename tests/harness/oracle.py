"""Smart verification oracle for spec-flow scenario runs.

Why: A scenario's ``tree`` is the *input* that drives the run; using it again as
the ground truth for a 1:1 equality check is circular — it proves the engine can
echo its own input, nothing more. This module replaces that crutch with an
OUTCOME oracle: it checks the realized ``RunResult`` against a small set of
declared, semantic invariants (depth bounds, reference anchor nodes, which
methodology episodes happened, control-flow loop counts, coverage floors, and
the research-before-implementation ordering). None of these re-assert the exact
tree shape — they assert that the run *behaved* correctly.

What: ``check(run_result, oracle_spec, summary) -> OracleReport`` evaluates each
declared expectation and returns a per-expectation pass/fail with a human
reason. ``render(report) -> str`` turns it into a readable markdown table.

Test: drive a scenario through ``run_project`` and assert ``check(...).ok`` is
True; mutate the oracle spec (e.g. demand a non-existent anchor or an impossible
``min_depth``) and assert ``check(...).ok`` becomes False. See
``tests/test_oracle.py``.

No absolute paths: everything is derived from the passed objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Map a declared episode name to the run-loop ``type`` it corresponds to (None
# means the episode is detected from events/tree, not from RunResult.loops).
EPISODE_TO_LOOP = {
    "clarify": "clarify",
    "review_critique": "review-fail",
    "drift_respec": "drift-respec",
    "drift_codefix": "drift-codefix",
    "revision": "revision-respec",
    # spike and contract are episodes detected from events, not loops
    "spike": None,
    "contract": None,
}

# Map a declared expected-loop key to the RunResult.loops ``type`` value.
LOOP_KEY_TO_TYPE = {
    "clarify": "clarify",
    "review_fail": "review-fail",
    "drift_respec": "drift-respec",
    "drift_codefix": "drift-codefix",
    "revision_respec": "revision-respec",
}


@dataclass
class Expectation:
    """A single checked invariant within the oracle report."""

    name: str
    ok: bool
    reason: str
    expected: Any = None
    actual: Any = None


@dataclass
class OracleReport:
    """Aggregate result: a list of expectations plus a roll-up ``ok`` flag."""

    expectations: list[Expectation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Why: a run passes the oracle only if EVERY expectation passed.
        Test: a report with one failing expectation must have ok == False."""
        return all(e.ok for e in self.expectations)

    @property
    def failures(self) -> list[Expectation]:
        return [e for e in self.expectations if not e.ok]

    def add(self, name, ok, reason, expected=None, actual=None) -> None:
        self.expectations.append(Expectation(name, ok, reason, expected, actual))


# -- realized-outcome derivations (from RunResult, never from the spec) --------

def _realized_depth(run_result) -> int:
    """Why: depth must be a derived OUTCOME, not a copy of the tree shape — we
    measure how deep the run actually decomposed.
    What: returns the maximum nesting level reached by the realized board,
    walking the tree the engine drove (root = depth 0).
    Test: a flat tree (root + leaf children only) yields 1; p4's deepest branch
    (catalog/listings/listing_editor) yields 3."""
    tree = run_result.project.get("tree") or {}

    def walk(node, depth):
        kids = node.get("children") or []
        if not kids:
            return depth
        return max(walk(c, depth + 1) for c in kids)

    return walk(tree, 0)


def _done_task_ids(run_result) -> set[str]:
    """Why: anchors must reach ``done`` to count — a task that exists but never
    completes is not real coverage.
    What: ids of tasks whose status is 'done'.
    Test: after a full run, 'L0' and every leaf node id are in this set."""
    return {tid for tid, t in run_result.tasks.items()
            if getattr(t, "status", "") == "done"}


def _loop_counts(run_result) -> dict[str, int]:
    """Why: control-flow loops are the methodology's feedback signal; counting
    them by type lets the oracle demand a minimum without re-listing the tree.
    What: maps loop ``type`` -> count over RunResult.loops.
    Test: p4 yields at least one each of clarify, review-fail, drift-respec,
    drift-codefix and revision-respec."""
    counts: dict[str, int] = {}
    for loop in run_result.loops:
        t = loop.get("type", "")
        counts[t] = counts.get(t, 0) + 1
    return counts


def _episode_present(run_result, episode: str) -> bool:
    """Why: an 'episode happened at least once' is a weaker, more meaningful
    claim than 'the tree contains node X'.
    What: True if the named methodology episode appears in the run — loop-backed
    episodes via RunResult.loops, spike/contract via the event stream.
    Test: 'spike' is present for any scenario carrying a research spike;
    'drift_codefix' is present only when a code-wrong drift was classified."""
    loop_type = EPISODE_TO_LOOP.get(episode)
    if loop_type is not None:
        return any(l.get("type") == loop_type for l in run_result.loops)
    if episode == "spike":
        return any(e.skill == "spec-research" and e.phase == "research"
                   for e in run_result.events)
    if episode == "contract":
        return any(e.gate == "contract_check" for e in run_result.events)
    return False


def _first_tick(run_result, *, skill: Optional[str] = None,
                phase: Optional[str] = None) -> Optional[int]:
    """Why: R9 (research-before-implementation) is an ordering invariant — we
    need the first tick of two event kinds to compare them.
    What: the lowest tick among events matching the given skill and/or phase, or
    None if none match.
    Test: for p4 the first 'spec-research' tick is below the first
    'spec-implement' tick."""
    ticks = [e.tick for e in run_result.events
             if (skill is None or e.skill == skill)
             and (phase is None or e.phase == phase)]
    return min(ticks) if ticks else None


# -- the checker ---------------------------------------------------------------

def check(run_result, oracle_spec: dict, summary: dict) -> OracleReport:
    """Why: turn a declared ``oracle:`` block into a verdict over the realized
    run, checking semantic invariants instead of structural equality.
    What: evaluates min/max depth bounds, anchor nodes reaching done, required
    episodes, minimum loop counts, coverage floors and research-before-impl;
    returns an OracleReport with one Expectation per check.
    Test: passing p4's own oracle yields report.ok == True; demanding a missing
    anchor or an impossible min_depth yields report.ok == False (see
    tests/test_oracle.py)."""
    spec = oracle_spec or {}
    rep = OracleReport()

    # 1. depth bounds — a range, not the exact tree depth
    depth = _realized_depth(run_result)
    if "min_depth" in spec:
        lo = int(spec["min_depth"])
        rep.add("min_depth", depth >= lo,
                f"realized depth {depth} {'>=' if depth >= lo else '<'} required {lo}",
                expected=f">= {lo}", actual=depth)
    if "max_depth" in spec:
        hi = int(spec["max_depth"])
        rep.add("max_depth", depth <= hi,
                f"realized depth {depth} {'<=' if depth <= hi else '>'} allowed {hi}",
                expected=f"<= {hi}", actual=depth)

    # 2. anchor nodes — semantic reference points that must exist AND reach done
    done = _done_task_ids(run_result)
    for anchor in spec.get("anchor_nodes", []) or []:
        exists = anchor in run_result.tasks
        is_done = anchor in done
        rep.add(f"anchor:{anchor}", exists and is_done,
                ("reached done" if exists and is_done
                 else ("exists but not done" if exists else "missing from board")),
                expected="done", actual=(getattr(run_result.tasks.get(anchor),
                                                  "status", "absent")))

    # 3. expected episodes — each must have happened at least once
    for episode in spec.get("expected_episodes", []) or []:
        present = _episode_present(run_result, episode)
        rep.add(f"episode:{episode}", present,
                "occurred" if present else "never occurred",
                expected=">= 1", actual="present" if present else "absent")

    # 4. expected loops — minimum counts of control-flow loops by type
    counts = _loop_counts(run_result)
    for key, want in (spec.get("expected_loops") or {}).items():
        loop_type = LOOP_KEY_TO_TYPE.get(key, key)
        have = counts.get(loop_type, 0)
        rep.add(f"loops:{key}", have >= int(want),
                f"{have} {'>=' if have >= int(want) else '<'} required {want}",
                expected=f">= {want}", actual=have)

    # 5. coverage — minimum skills/profiles exercised (prefer the run, fall back
    # to summarize_trace's *_missing lists for a cross-check)
    cov = spec.get("coverage") or {}
    if "min_skills" in cov:
        used = len(run_result.skills_used)
        rep.add("coverage:skills", used >= int(cov["min_skills"]),
                f"{used} skills used (missing per trace: "
                f"{summary.get('skills_missing', [])})",
                expected=f">= {cov['min_skills']}", actual=used)
    if "min_profiles" in cov:
        used = len(run_result.profiles_used)
        rep.add("coverage:profiles", used >= int(cov["min_profiles"]),
                f"{used} profiles used (missing per trace: "
                f"{summary.get('profiles_missing', [])})",
                expected=f">= {cov['min_profiles']}", actual=used)

    # 6. research_before_impl — R9 ordering: first research precedes first impl
    if spec.get("research_before_impl"):
        first_research = _first_tick(run_result, skill="spec-research")
        first_impl = _first_tick(run_result, skill="spec-implement")
        ok = (first_research is not None and first_impl is not None
              and first_research < first_impl)
        rep.add("research_before_impl", ok,
                f"first research tick={first_research}, first impl tick={first_impl}",
                expected="research < impl",
                actual=f"{first_research} vs {first_impl}")

    return rep


def render(report: OracleReport) -> str:
    """Why: a reviewer needs a readable verdict, not a raw dataclass dump.
    What: renders the OracleReport as a markdown table with a roll-up header.
    Test: the output contains a row per expectation and a PASS/FAIL summary
    line."""
    head = "✅ PASS" if report.ok else "❌ FAIL"
    n_ok = sum(1 for e in report.expectations if e.ok)
    n = len(report.expectations)
    lines = [
        f"## Oracle verdict — {head} ({n_ok}/{n} expectations)",
        "",
        "| Expectation | Verdict | Expected | Actual | Reason |",
        "|---|---|---|---|---|",
    ]
    for e in report.expectations:
        mark = "✅" if e.ok else "❌"
        lines.append(
            f"| `{e.name}` | {mark} | {e.expected} | {e.actual} | {e.reason} |")
    return "\n".join(lines) + "\n"
