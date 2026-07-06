"""STAGE 37 (S37): the live ir.json grows after EVERY closed node on the
current depth — branch OR leaf — not only after a leaf is REALIZED.

Why (v167 audit): the ONLY call to `_write_ir_incremental` lived in the leaf
arm of `_visit` (labelled "leaf realized"). On `--depth spec` a leaf still
reaches that arm (v167 trace shows 6 "leaf realized" IR dumps), so the leaf
path was fine — but a BRANCH node closes its spec stage (its OpenAPI/contract
is materialised at the review gate) WITHOUT ever dumping the IR. The live
artifact therefore stayed ABSENT until the first leaf closed: v167 checkpoints
001/002 already carry specs/ and contracts/ for the branch nodes yet have NO
ir.json at all, so the dashboard IR tab was empty for the whole opening of the
run. The promise (I1/I2/I3) is a LIVE incremental IR that grows from the first
CLOSED node regardless of `--depth`; a branch that closed its spec stage must
move the IR too.

The fix: after a branch node closes (its integrate lands), call
`_write_ir_incremental` the same way the leaf arm does — under the I1 lock
(`_write_ir_locked`), idempotent (I2 dedups a repeated dump of the same node),
never touching the final full `_write_ir` dump. So the IR gains a machine
carrier from the FIRST closed node, not only from the first realized leaf.

Both directions (v151 lesson):
  * S37.1 (the hole): the branch arm of `_visit` carries an incremental IR
    write — RED before the fix, since only the leaf arm did.
  * S37.2 (no regression): the leaf arm KEEPS its incremental write, so the
    fix adds the branch dump without weakening the realized-leaf one (I1
    S14.6, I2 S25, I3 S26 all stay green).
  * S37.3 (idempotent seam): the branch dump goes through the SAME locked
    writer alias, never a second raw writer — no new race, no bypass of I1.
"""

import inspect

import spec_flow_runner as sfr


def _visit_arms():
    """Split _visit source into its branch arm and its leaf arm.

    Why: the incremental-IR promise is per-ARM — the leaf arm had the dump,
    the branch arm did not. Slicing on the two arm markers lets each assert
    speak about exactly one arm.
    What: returns (branch_arm_src, leaf_arm_src).
    Test: this module's asserts below.
    """
    src = inspect.getsource(sfr.Engine._visit)
    i_branch = src.index('if verdict == "branch":')
    i_leaf = src.index("self._leaf_pipeline(")
    return src[i_branch:i_leaf], src[i_leaf:]


def test_branch_arm_dumps_the_live_ir_on_close():
    """S37.1: closing a BRANCH node must move the live IR.

    Why: v167 (depth spec) left ir.json absent through checkpoints 001/002 —
    branch spec stages had closed (specs/ + contracts/ present) but nothing
    dumped the IR until the first leaf. What: the branch arm of _visit calls
    the incremental writer. Test: this assert, RED before the S37 fix.
    """
    branch_arm, _leaf_arm = _visit_arms()
    assert "_write_ir_incremental" in branch_arm, (
        "the branch arm of _visit must dump the live IR after the node "
        "closes — v167 kept ir.json absent until the first leaf because "
        "only the leaf arm dumped, so a spec-depth run showed an empty IR "
        "tab while branch OpenAPI/contracts already existed")


def test_leaf_arm_keeps_its_incremental_dump():
    """S37.2: the realized-leaf dump (I1 S14.6) is not lost by the S37 fix.

    Why: adding the branch dump must not remove the leaf one. What: the leaf
    arm still calls the incremental writer. Test: this assert, GREEN before
    and after the fix.
    """
    _branch_arm, leaf_arm = _visit_arms()
    assert "_write_ir_incremental" in leaf_arm, (
        "the leaf arm must KEEP its incremental IR dump (I1 S14.6) — the "
        "S37 branch dump is additive, never a replacement")


def test_incremental_writer_is_the_locked_alias():
    """S37.3: the incremental dump goes through the I1 locked writer.

    Why: I1 (S14.6a) made every ir.json write hold `_ir_write_lock` so no
    two builders interleave; a new dump site must reuse that alias, not open
    a second raw writer. What: `_write_ir_incremental` delegates to
    `_write_ir` (the locked writer). Test: this assert.
    """
    inc = inspect.getsource(sfr.Engine._write_ir_incremental)
    assert "_write_ir(" in inc, (
        "the incremental dump must delegate to the locked _write_ir alias "
        "(I1) so the branch dump cannot race the leaf dump")
