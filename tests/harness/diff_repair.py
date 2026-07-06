"""Diff-based repair (#1 / П3).

A failed leaf used to be repaired by REWRITING whole files: the model
re-emits src/<fn>.py and tests/test_<fn>.py from scratch. That clobbers
working code (a one-line bug triggers a full re-roll that can drop unrelated
correct lines) and costs a whole-file generation each round.

Instead the repair worker emits SEARCH/REPLACE diff blocks against the
CURRENT file. Each block is applied only if its search text matches the
file EXACTLY ONCE — a non-applicable block (text gone, or ambiguous) is
REFUSED without a write, so a stale diff never corrupts the file. Surgical,
cheap, regression-resistant.

Block format (the repair prompt asks for exactly this):

    FILE: src/cart.py
    <<<<<<< SEARCH
    def add(payload):
    =======
    def add(payload, query):
    >>>>>>> REPLACE

An empty SEARCH body means "replace the whole file" (a create / full
rewrite escape hatch the model can still use when a diff won't express the
change). Test: tests/test_diff_repair.py.

Recorded reason — why this is hand-rolled, not a diff library (node H1b).
A generic patch library (diff-match-patch, unidiff, python-patch) nominally
covers "apply a patch to text", so the NIH is called out here so it can never
be silent. It is deliberate: none of those libraries gives the domain
behaviour this applier exists for.
  * The model-facing block FORMAT contract. The unit is not a unified diff but
    an LLM-friendly `FILE:` + `<<<SEARCH / === / >>>REPLACE` grammar (see
    ``_BLOCK``) that the repair prompt is instructed to emit, tolerant of any
    run of markers >= 3 (a model that emits 5 or 8 of them still parses).
    diff-match-patch / unidiff parse patch text, not this grammar.
  * Empty SEARCH = whole-file replace — a create / full-rewrite escape hatch
    with no diff-library equivalent.
  * Refusal, not fuzzy application. A block applies ONLY on an EXACTLY-ONCE
    match; 0 (gone) or >1 (ambiguous) is REFUSED without a write so a stale
    diff never corrupts the file. diff-match-patch does the opposite (fuzzy
    match_main / patch_apply with a match threshold); unidiff needs exact line
    offsets. The refusal IS the safety property.
  * Write-door integration (``apply_repair``): an ``allowed`` path allowlist,
    per-file accumulation of composed edits, and a staged files-to-write dict
    a refused block leaves untouched — the seam the harness write door reads.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# FILE: <path> then a SEARCH/REPLACE block. The markers tolerate any run of
# <, =, > of length >= 3 so a model that emits 5 or 8 of them still parses.
_BLOCK = re.compile(
    r"FILE:[ \t]*(?P<file>\S+)[ \t]*\n"
    r"<{3,}[ \t]*SEARCH[ \t]*\n"
    r"(?P<search>.*?)\n?"
    r"={3,}[ \t]*\n"
    r"(?P<replace>.*?)\n?"
    r">{3,}[ \t]*REPLACE",
    re.DOTALL)


def parse_blocks(text: str) -> list:
    """Extract every well-formed SEARCH/REPLACE block from a model reply.
    Returns [{'file', 'search', 'replace'}] in document order; malformed or
    absent blocks simply yield []."""
    out = []
    for m in _BLOCK.finditer(text or ""):
        out.append({"file": m.group("file").strip(),
                    "search": m.group("search"),
                    "replace": m.group("replace")})
    return out


def apply_block(content: str, search: str, replace: str) -> tuple:
    """Apply one block to a file's current text. Returns (ok, result):
      * ok=True  → result is the new content;
      * ok=False → result is the refusal reason.
    Applicability rule: an empty search replaces the WHOLE file; otherwise
    the search must appear EXACTLY ONCE (0 = gone, >1 = ambiguous → refuse,
    never a blind write)."""
    if search.strip() == "":
        return True, replace
    n = content.count(search)
    if n == 0:
        return False, "search text not found"
    if n > 1:
        return False, f"search text ambiguous ({n} matches)"
    return True, content.replace(search, replace, 1)


def apply_repair(read_file, blocks: list,
                 allowed: Optional[set] = None) -> dict:
    """Apply a list of blocks. ``read_file(rel)`` returns the current text of
    a workspace-relative path (or '' if absent). ``allowed`` restricts which
    paths may be touched (None = any). Blocks are applied IN ORDER and
    accumulate per file, so two edits to the same file compose.

    Returns {'files': {rel: new_content}, 'applied': [..], 'refused':
    [(rel, reason)]}. Only files in 'files' should be written; a refused
    block leaves that file untouched."""
    staged: dict = {}
    applied, refused = [], []
    for b in blocks:
        rel = b["file"]
        if allowed is not None and rel not in allowed:
            refused.append((rel, "path not permitted"))
            continue
        cur = staged.get(rel)
        if cur is None:
            cur = read_file(rel) or ""
        ok, res = apply_block(cur, b["search"], b["replace"])
        if ok:
            staged[rel] = res
            applied.append(rel)
        else:
            refused.append((rel, res))
    return {"files": staged, "applied": applied, "refused": refused}
