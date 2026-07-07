"""Audit stage S51 (node Q11, plan 2026-07-06T20-15): the IR jsonschema must
accept `children` as the list of child node ids it actually is. The node schema
declared `children: {type: object}`, but build_ir writes (and all engine code
reads) `children` as an ARRAY of id strings (`entry["children"] = [str(id) …]`,
`isinstance(node["children"], list)`, `sorted(node["children"])`). The mismatch
was latent — `children` was usually absent (None), so it never validated —
until Q7 (publish the tree before the visit) made every node carry `children`
(a list, possibly empty). Then EVERY node reddened `ir_jsonschema` with
"[…] is not of type 'object'" (8 errors on v176), the run "finished with an
error" while its graph was otherwise clean (closed-world 0). S51 corrects the
schema to match the format ("children?: [child node ids]").

  * S51.1 — a node with `children` as a list of id strings validates clean.
  * S51.2 — an empty `children: []` validates clean (every node carries it post-Q7).
  * S51.3 — a bogus non-string child is still rejected (the fix is precise, not
    permissive).

Deterministic: jsonschema oracle on hand-built IRs, no run.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_ir  # noqa: E402


def _ir(node):
    return {"format": spec_ir.IR_FORMAT, "product": {}, "nodes": {"L0": node}}


def test_children_list_of_ids_validates():
    errs = spec_ir.jsonschema_errors(_ir({"children": ["db", "core"],
                                          "files": []}))
    assert not errs, "children as a list of node ids must validate: %r" % errs


def test_empty_children_validates():
    errs = spec_ir.jsonschema_errors(_ir({"children": [], "files": []}))
    assert not errs, "every node carries children post-Q7; [] must validate: %r" % errs


def test_children_object_is_rejected():
    # the OLD (wrong) shape must NOT be silently accepted — precise fix.
    errs = spec_ir.jsonschema_errors(_ir({"children": {"a": 1}, "files": []}))
    assert errs, "children must be an array, not an object"


def test_children_non_string_item_rejected():
    errs = spec_ir.jsonschema_errors(_ir({"children": [123], "files": []}))
    assert errs, "a child id must be a string"
