"""Variant A: static namespace partition — two leaves can NEVER write
the same src/<name>.py by construction. Two distinct node ids that snake
to the same base get a deterministic short-hash suffix; the same id
always resolves to the same module (resume-safe)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


def _engine(tmp_path):
    # a bare Engine instance is enough to exercise _module_for
    e = eng.Engine(workspace=str(tmp_path / "wk"))
    e._module_names = {}
    import threading
    e._module_lock = threading.Lock()
    e.events = []
    return e


def test_same_id_is_stable(tmp_path):
    e = _engine(tmp_path)
    a = e._module_for("seller_onboarding")
    b = e._module_for("seller_onboarding")
    assert a == b == "seller_onboarding"


def test_colliding_ids_get_distinct_modules(tmp_path):
    e = _engine(tmp_path)
    # both snake to 'seller_opt_in'
    m1 = e._module_for("seller-opt-in")
    m2 = e._module_for("seller opt in")
    assert m1 == "seller_opt_in"
    assert m2 != m1 and m2.startswith("seller_opt_in_")
    # the loser's suffix is a deterministic hash of its FULL id
    m2_again_engine = _engine(tmp_path)
    m2_again_engine._module_for("seller-opt-in")          # same winner first
    assert m2_again_engine._module_for("seller opt in") == m2


def test_collision_emits_partition_event(tmp_path):
    e = _engine(tmp_path)
    e._module_for("buyer-cart")
    e._module_for("buyer cart")
    parts = [ev for ev in e.events if getattr(ev, "verdict", "") == "PARTITIONED"]
    assert parts and "namespace collision avoided" in parts[0].action


def test_no_collision_no_event(tmp_path):
    e = _engine(tmp_path)
    e._module_for("alpha")
    e._module_for("beta")
    assert not [ev for ev in e.events
                if getattr(ev, "verdict", "") == "PARTITIONED"]


def test_live_run_every_leaf_has_a_unique_module(tmp_path):
    # a real run whose decomposer hands back sibling ids that collide
    # under _snake must still give every leaf its own src file
    def dec(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BRANCH), "children": [
                {"id": "pay-flow", "title": "Pay flow"},
                {"id": "pay flow", "title": "Pay  flow alt"}]}
        return {"metrics": dict(_LEAF)}

    # the engine's tree-level dedup gate already collapses near-identical
    # sibling ids; _module_for is the SECOND line for ids that pass dedup
    # but still snake alike. The end-to-end guarantee A delivers: across a
    # whole run, every assigned module name is UNIQUE (no src file shared)
    res = eng.run_project(
        {"name": "ns-case", "goal": "g", "target": "x",
         "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                    "human_in_loop": False, "involves_outreach": False,
                    "consent_obtained": True, "legal_exposure": False,
                    "legality_reviewed": True}},
        workspace=str(tmp_path / "wk"), depth="scaffold",
        agents={"decomposer": dec})
    mods = list(res.module_names.values())
    assert mods, "the run must assign module names"
    assert len(mods) == len(set(mods)), \
        "every node's module name must be unique — no two share a src file"
