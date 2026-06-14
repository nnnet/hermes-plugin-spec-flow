"""Battle test — OpenSpec change deltas → respec nodes (roadmap B3).

OpenSpec describes a brownfield change as a spec delta with ADDED / MODIFIED /
REMOVED requirement blocks. spec-flow maps each onto a respec ACTION: ADDED ->
create, MODIFIED/RENAMED -> respec (version-bump + re-derive), REMOVED ->
retire. Pure parser + mapper, verified WITHOUT Hermes.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

DELTA_MD = """# Spec Delta: checkout v2

## ADDED Requirements
### Requirement: Guest Checkout
The system SHALL allow checkout without an account.

#### Scenario: anonymous buyer
- WHEN a guest submits an order THEN it SHALL be accepted.

## MODIFIED Requirements
### Requirement: Order Totals
Totals SHALL be decimal-safe strings, not integers.

## REMOVED Requirements
### Requirement: Legacy Coupon Codes
Deprecated coupon engine is removed.
"""


def test_parse_delta_ops_and_names(plugin):
    deltas = plugin.tools.parse_openspec_delta(DELTA_MD)
    ops = [(d["op"], d["name"]) for d in deltas]
    assert ("ADDED", "Guest Checkout") in ops
    assert ("MODIFIED", "Order Totals") in ops
    assert ("REMOVED", "Legacy Coupon Codes") in ops
    assert len(deltas) == 3


def test_delta_body_captured(plugin):
    deltas = {d["name"]: d for d in plugin.tools.parse_openspec_delta(DELTA_MD)}
    assert "decimal-safe" in deltas["Order Totals"]["body"]
    assert "anonymous buyer" in deltas["Guest Checkout"]["body"]


def test_slug_ids(plugin):
    deltas = {d["name"]: d for d in plugin.tools.parse_openspec_delta(DELTA_MD)}
    assert deltas["Guest Checkout"]["id"] == "guest_checkout"


def test_map_to_respec_actions(plugin):
    nodes = plugin.tools.map_deltas_to_respec(
        plugin.tools.parse_openspec_delta(DELTA_MD))
    by = {n["name"]: n for n in nodes}
    assert by["Guest Checkout"]["action"] == "create"
    assert by["Order Totals"]["action"] == "respec"
    assert by["Legacy Coupon Codes"]["action"] == "retire"


RENAMED_MD = """# Spec Delta

## RENAMED Requirements
- FROM: `### Requirement: Coupon Codes`
- TO: `### Requirement: Promotions`
"""


def test_renamed_from_to_pair(plugin):
    deltas = plugin.tools.parse_openspec_delta(RENAMED_MD)
    assert len(deltas) == 1
    d = deltas[0]
    assert d["op"] == "RENAMED"
    assert d["name"] == "Promotions"            # the respec node is the NEW name
    assert "Coupon Codes" in d["body"]          # provenance keeps the old one
    nodes = plugin.tools.map_deltas_to_respec(deltas)
    assert nodes[0]["action"] == "respec"


def test_import_tool_counts(plugin):
    out = json.loads(plugin.tools._handle_openspec_import({"delta": DELTA_MD}))
    assert out["parsed"] == 3
    assert out["counts"] == {"ADDED": 1, "MODIFIED": 1, "REMOVED": 1}
    assert len(out["respec_nodes"]) == 3


def test_import_reads_file(plugin, tmp_path):
    p = tmp_path / "delta.md"
    p.write_text(DELTA_MD, encoding="utf-8")
    out = json.loads(plugin.tools._handle_openspec_import({"delta_md": str(p)}))
    assert out["parsed"] == 3


def test_import_rejects_non_delta(plugin):
    out = json.loads(plugin.tools._handle_openspec_import(
        {"delta": "# just a doc\n\nno op headers"}))
    assert "error" in out


def test_tool_registered(plugin):
    assert "openspec_import" in plugin.reg.tools
