"""Per-node truncation collector (doctor data source for silent_truncation).

When the harness drops part of an input (a file inlined into a prompt exceeds
the embed limit), the cut is recorded HERE keyed by the node currently being
built, so the engine can later hand the markers to the doctor as
evidence['dropped']. Without this, a truncation-caused failure is invisible —
the detector has nothing to fire on.

The 'current node' is carried by a ContextVar the engine sets around a worker
call, so producers (_inline_file) need no node argument threaded through them.
In-process only (offline harness); a no-op outside any node scope."""
import contextlib
import contextvars

# node id whose work is running right now ('' = outside any node scope)
_CURRENT: contextvars.ContextVar = contextvars.ContextVar(
    "spec_flow_truncation_node", default="")

# node id -> list of drop markers ("rel:dropped_chars") collected this run
_DROPS: dict = {}


@contextlib.contextmanager
def node_scope(nid: str):
    """Mark drops recorded inside the block as belonging to ``nid``."""
    token = _CURRENT.set(str(nid or ""))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def record(marker: str) -> None:
    """A producer cut some input — note it against the current node (no-op
    outside any node scope, so library code never crashes)."""
    nid = _CURRENT.get()
    if not nid:
        return
    _DROPS.setdefault(nid, []).append(str(marker))


def drain(nid: str) -> list:
    """The markers collected for ``nid`` (and forget them — one diagnosis per
    truncation episode, not a growing tally)."""
    return _DROPS.pop(str(nid or ""), [])


def reset() -> None:
    """Clear all collected markers (start-of-run / test isolation)."""
    _DROPS.clear()
