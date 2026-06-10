---
name: spec-requirements
description: >
  Use at the L0/L1 root of a spec-flow project (phases 0-1): write the project
  constitution, capture EARS-style requirements, run a clarify gate for
  ambiguities, and produce an explicit Out-of-scope list. The output is the
  approved input every lower level traces back to.
---

# spec-requirements

You own the **top of the tree**: turn a project goal into a frozen, testable
requirements baseline.

## Steps

1. **Constitution.** Write/append `constitution.md` — the non-negotiable rules
   (security, data handling, tech constraints, what must never happen). Every
   spec at every level is later checked against this.
2. **EARS requirements.** Capture requirements in EARS form ("When <trigger>,
   the system shall <response>"). Give each a stable id `REQ-n` — lower levels
   trace to these ids.
3. **Clarify gate.** For every ambiguity that changes scope or design, call
   `clarify` (it forces sequential execution and waits for the answer). Do not
   guess past a scope-affecting unknown.
4. **Out-of-scope.** State explicitly what is NOT being built. This bounds
   later `Traces-to` checks (orphan in-scope item = scope creep).
5. **Quality checklist.** Requirements are testable, non-overlapping,
   id-stable, and consistent with the constitution.

Close with `kanban_complete(summary, metadata={requirements:[REQ-...],
constitution_path, out_of_scope})`. The L1 decomposition node reads this as its
approved input.
