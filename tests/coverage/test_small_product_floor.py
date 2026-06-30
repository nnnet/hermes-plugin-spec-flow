"""#122 — small-product floor: a micro-service is built as ONE atomic leaf.

A product-depth run whose human-text contract declares few routes must NOT let
the decomposer shatter it into rival whole-app modules (v125 root: 8 leaves,
3 rival `wsgi_app`/`application` entries, e2e RED, 343 calls). The root product
node is forced atomic so a single module owns every route + its storage and the
engine synthesizes the entry. Larger products keep decomposing.

These tests pin the deterministic predicate `_small_product_root` — route count
comes from the contract, never from model output.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path, depth=sfr.DEPTH_PRODUCT, n_routes=4):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=depth)
    eng._small_product_routes = 5
    # a contract declaring exactly n_routes (json_roundtrip = POST+GET = 2,
    # ok_route = 1, html_route = 1 → 4; extra GET routes add 1 each)
    boot = {"json_roundtrip": "/notes", "ok_route": "/health",
            "html_route": "/ui"}
    extra = [["GET", "/x%d" % i] for i in range(max(0, n_routes - 4))]
    eng._product_contract = lambda: {"entry": "src/app.py", "boot": boot,
                                     "routes": extra}
    return eng


def _root(metrics=None):
    return {"id": "L0", "title": "Notes service",
            "metrics": metrics or {"open_decisions": 0, "estimated_loc": 40}}


def test_small_root_forced_atomic(tmp_path):
    eng = _engine(tmp_path, n_routes=4)
    assert eng._small_product_root(_root(), 0, None) == 4


def test_large_product_keeps_decomposing(tmp_path):
    # 6 routes > threshold 5 → not collapsed (p4/p5 protection)
    eng = _engine(tmp_path, n_routes=6)
    assert eng._small_product_root(_root(), 0, None) == 0


def test_child_node_never_collapsed(tmp_path):
    eng = _engine(tmp_path, n_routes=4)
    assert eng._small_product_root(_root(), 1, "L0") == 0      # depth 1
    assert eng._small_product_root(_root(), 0, "L0") == 0      # has a parent


def test_open_decision_blocks_collapse(tmp_path):
    eng = _engine(tmp_path, n_routes=4)
    node = _root({"open_decisions": 2, "estimated_loc": 40})
    assert eng._small_product_root(node, 0, None) == 0


def test_explicit_atomic_claim_is_left_alone(tmp_path):
    eng = _engine(tmp_path, n_routes=4)
    node = _root()
    node["atomic"] = True
    assert eng._small_product_root(node, 0, None) == 0


def test_floor_off_when_threshold_zero(tmp_path):
    eng = _engine(tmp_path, n_routes=4)
    eng._small_product_routes = 0
    assert eng._small_product_root(_root(), 0, None) == 0


def test_non_product_depth_does_not_collapse(tmp_path):
    # a spec/scaffold run is not building a runnable product → floor is inert
    eng = _engine(tmp_path, depth=sfr.DEPTH_SPEC, n_routes=4)
    assert eng._small_product_root(_root(), 0, None) == 0


def test_non_http_product_not_collapsed(tmp_path):
    # a library/CLI project yields an empty contract (0 routes) → never collapsed
    eng = _engine(tmp_path, n_routes=4)
    eng._product_contract = lambda: {}
    assert eng._small_product_root(_root(), 0, None) == 0


def test_atomic_field_stays_json_serializable(tmp_path):
    import json
    eng = _engine(tmp_path, n_routes=4)
    node = _root()
    if eng._small_product_root(node, 0, None):
        node["atomic"] = True
    json.dumps(node)        # must not raise (bool, not set)
