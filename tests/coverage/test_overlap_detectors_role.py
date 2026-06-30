"""Phase 4 — the surface-overlap detectors are CONFINED to their role.

The similarity detectors (_amend_*, _AMEND_DETECTORS, _dup_surface_findings)
exist for ONE purpose: routing a LATE requirement into the module that already
owns its surface (anti-duplication) and warning when a late spec restates an
existing surface. They MUST NOT take part in route -> handler binding or entry
assembly — that path binds by the EXACT canonical handler name (Phase 1) with
the name-similarity scoring deleted from the resolver (Phase 3). A guessed
binding is a false-green risk; a guessed anti-dup routing is recoverable.

This test LOCKS the separation statically: the binding/assembly functions must
reference no overlap detector, and every detector consumer must be a late-
requirement / anti-dup site. If a future change wires a detector back into the
build path, this fails loudly.
"""

import inspect
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


# names that constitute the overlap-detector machinery
_DETECTOR_TOKENS = (
    "_amend_routes", "_amend_symbols", "_amend_tokens", "_amend_surfaces",
    "_AMEND_DETECTORS", "_amd_", "_dup_surface_findings", "_amend_find_owner",
)


def _src(fn):
    return inspect.getsource(fn)


def test_resolver_references_no_overlap_detector():
    body = _src(sfr.Engine._resolve_route_handlers)
    hits = [t for t in _DETECTOR_TOKENS if t in body]
    assert not hits, (
        "route->handler resolver must bind by canonical name only, not by "
        "overlap detectors: " + ", ".join(hits))


def test_entry_synthesis_references_no_overlap_detector():
    body = _src(sfr.Engine._synthesize_entry_code)
    hits = [t for t in _DETECTOR_TOKENS if t in body]
    assert not hits, (
        "entry assembly must be deterministic, not detector-driven: "
        + ", ".join(hits))


def test_leaf_handler_gate_references_no_overlap_detector():
    # Phase 2's leaf gate binds by canonical name too — it must not regress into
    # similarity matching either.
    body = _src(sfr.Engine._leaf_handler_gate)
    hits = [t for t in _DETECTOR_TOKENS if t in body]
    assert not hits, "leaf handler gate must be canonical-name only: " + ", ".join(hits)


def test_detectors_are_used_only_at_late_req_sites():
    """Every method that consults the overlap detectors must be an anti-dup /
    late-requirement routing site (guarded by `_late_req` or the amend flag),
    never a build-path function. We assert by the known allowed consumer set;
    a new consumer outside it must be added here CONSCIOUSLY (and be a routing
    site), which is the whole point of the lock."""
    allowed = {
        "_amend_target", "_amend_find_owner", "_late_req_scope_findings",
        "_decomposer_ctx", "_surface_modules",
        # _review_gate drives the late-req scope lint (anti-dup) and the
        # late_req_contract hand-off — a routing site, not a build-path one.
        "_review_gate",
    }
    detector_call = re.compile(r"_amend_(?:routes|symbols|tokens|surfaces)\b"
                               r"|_AMEND_DETECTORS\b|_dup_surface_findings\b"
                               r"|_amend_find_owner\b")
    offenders = []
    for name, member in inspect.getmembers(sfr.Engine, inspect.isfunction):
        if name in allowed:
            continue
        try:
            body = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if detector_call.search(body):
            offenders.append(name)
    assert not offenders, (
        "overlap detectors leaked into non-routing methods: "
        + ", ".join(sorted(offenders)))
