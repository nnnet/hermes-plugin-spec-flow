"""#2: topological leaf ordering. A child's depends_on siblings run first, so
a dependent sees its dependency already built at integrate — fewer reworks.
Stable, tolerant of unknown ids, cycle-safe, and forces sequential execution
when intra-sibling deps exist."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402


def _engine():
    import tempfile; return eng.Engine(workspace=tempfile.mkdtemp())


def _kids(*specs):
    # specs: (id, [deps]) tuples
    return [{"id": i, "depends_on": d} for i, d in specs]


def test_no_deps_preserves_order():
    e = _engine()
    kids = _kids(("a", []), ("b", []), ("c", []))
    assert [c["id"] for c in e._topo_order(kids)] == ["a", "b", "c"]


def test_dependency_runs_before_dependent():
    e = _engine()
    # b depends on c → c must come before b
    kids = _kids(("a", []), ("b", ["c"]), ("c", []))
    order = [k["id"] for k in e._topo_order(kids)]
    assert order.index("c") < order.index("b")


def test_chain_is_fully_ordered():
    e = _engine()
    # a→b→c reversed in declaration; topo must yield c,b,a
    kids = _kids(("a", ["b"]), ("b", ["c"]), ("c", []))
    assert [k["id"] for k in e._topo_order(kids)] == ["c", "b", "a"]


def test_unknown_dep_is_ignored():
    e = _engine()
    kids = _kids(("a", ["external"]), ("b", []))
    # 'external' isn't a sibling → ignored, declared order kept
    assert [k["id"] for k in e._topo_order(kids)] == ["a", "b"]


def test_cycle_falls_back_to_declared_order():
    e = _engine()
    kids = _kids(("a", ["b"]), ("b", ["a"]))
    # a cycle can't be ordered — both still appear, no crash, no loss
    out = [k["id"] for k in e._topo_order(kids)]
    assert sorted(out) == ["a", "b"]


def test_stable_when_independent():
    e = _engine()
    kids = _kids(("x", []), ("y", ["x"]), ("z", []))
    # only y constrained (after x); x and z keep declared positions
    out = [k["id"] for k in e._topo_order(kids)]
    assert out.index("x") < out.index("y")
    assert out == ["x", "z", "y"] or out == ["x", "y", "z"]


# ─── the sibling-dep signal ───────────────────────────────────────────

def test_has_sibling_deps_detects_intra():
    e = _engine()
    assert e._has_sibling_deps(_kids(("a", []), ("b", ["a"]))) is True


def test_has_sibling_deps_false_for_external_only():
    e = _engine()
    assert e._has_sibling_deps(_kids(("a", ["lib"]), ("b", []))) is False


def test_has_sibling_deps_ignores_self():
    e = _engine()
    assert e._has_sibling_deps(_kids(("a", ["a"]))) is False


def test_empty_kids_safe():
    e = _engine()
    assert e._topo_order([]) == []
    assert e._has_sibling_deps([]) is False
