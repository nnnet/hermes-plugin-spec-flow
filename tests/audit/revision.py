#!/usr/bin/env python3
"""Audit LAYER 3 — periodic LLM code revision against the charter.

Why: layers 1-2 guard KNOWN failure shapes; this reviewer walks the whole
engine source against the design PRINCIPLES (tests/audit/CHARTER.md) so a
violation no stage was written for yet still surfaces cheaply, offline from
any live run.

What: map-reduce over `spec_flow_runner.py` — the file is cut into semantic
chunks (~300-400 lines, split at def/class boundaries); each chunk plus the
charter goes to one LLM call that returns JSON findings
{principle, location, description, severity}; a final synthesis call dedups,
ranks and picks the top-10. Output: tests/audit/revision-report.md.

Test: `python3 tests/audit/revision.py --max-llm-calls 3` reviews at most
2 chunks + 1 synthesis and writes a report noting the skipped remainder;
a dead gateway degrades every chunk to a skip note, never a crash.

All LLM traffic goes through the ONE door `harness.llm_backend.ask()`
(mimo leads, claude/sonnet is the fallback — mirrors the p6 workers block).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]          # tests/
_REPO = _TESTS.parent                                 # spec-flow/
sys.path.insert(0, str(_TESTS))

from harness import llm_backend as lb  # noqa: E402

TARGET_DEFAULT = _REPO / "spec_flow_runner.py"
CHARTER = Path(__file__).resolve().parent / "CHARTER.md"
REPORT = Path(__file__).resolve().parent / "revision-report.md"

# Worker configuration mirrors tests/scenarios/p6_micro_notes.yaml `workers:`:
# every provider rides Bifrost (claude included, via its anthropic provider →
# Meridian), mimo leads every chain, claude/sonnet is the reliable fallback.
WORKERS_CFG: dict = {
    "backend": "openai",
    "base_url": "http://127.0.0.1:8080/v1",
    "claude_gateway": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model_map": {
            "claude/sonnet": "anthropic/claude-sonnet-4-6",
            "sonnet": "anthropic/claude-sonnet-4-6",
        },
    },
    "retries": 3,
    # review prompts are BIG (charter + up to ~800 numbered lines) and the
    # findings generation is long — 75s (the p6 leaf default) times out a
    # healthy sonnet mid-generation and retires it after ONE miss; measured:
    # a 50KB chunk over the serialized claude lane runs ~150-200s, so 300s
    # lets a real review finish while still bailing a genuinely hung call.
    "timeout": 300,
    "provider_concurrency": {"claude": 1, "anthropic": 1},
    "provider_timeout": {"claude": 300, "anthropic": 300},
    "cycle_models": False,
    "quota_wait_s": 20,
    "quota_retries": 1,
    # the env floor's terminal fallback is a bare "haiku" (no provider prefix)
    # which the Bifrost catalog rejects with 400; the chain already carries
    # its own fallback (claude/sonnet), so the extra rotation is disabled.
    "fallback_model": "",
    "providers": [
        {"name": "xiaomimimo", "kind": "openai", "model_prefix": "xiaomimimo/",
         "requests_per_day": 1000},
        {"name": "claude-cli", "kind": "claude", "model_prefix": "claude/"},
    ],
    "defaults": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"]},
    "reviewer": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"]},
}

_SEVERITIES = ("high", "medium", "low")

_MAP_SYSTEM = """You are a rigorous code auditor. You review ONE chunk of a \
Python orchestration engine against a design charter. Report only REAL \
violations of the charter principles you can point to in the given lines — \
no style nits, no speculation about code you cannot see. If the chunk shows \
no violation, return an empty JSON array.

Respond with ONLY a JSON array (no prose, no markdown fence), each element:
{"principle": "P1".."P8", "location": "spec_flow_runner.py:<line>",
 "description": "<one concise sentence>", "severity": "high|medium|low"}
Line numbers are given as prefixes in the chunk; cite them exactly."""

_SYNTH_SYSTEM = """You are the audit synthesizer. You receive raw JSON \
findings collected from independent chunk reviews of one engine file. \
Deduplicate (same principle + same root cause = one finding), discard \
non-findings, rank by (severity, breadth of impact), and output the TOP \
findings as a Markdown numbered list — at most 10 items. Each item: \
`**[P#/severity]** location — description` plus, when duplicates were \
merged, `(also at ...)`. Output ONLY the Markdown list."""


def _semantic_chunks(lines: list[str], target: int = 380,
                     floor: int = 300) -> list[tuple[int, int]]:
    """Cut the file into (start_line, end_line) 1-based inclusive spans of
    roughly `target` lines, breaking only at semantic boundaries (top-level
    def/class, method def, or section-comment rules) once `floor` is reached.

    Why: a chunk that slices a function in half makes the reviewer hallucinate
    about the missing half. Test: spans cover every line exactly once and no
    span (except possibly the last) is shorter than `floor` lines.
    """
    boundary = re.compile(r"^(class |def |    def |# ─|#  ─)")
    spans: list[tuple[int, int]] = []
    start = 1
    i = 0
    while i < len(lines):
        size = i + 1 - (start - 1)
        if size >= floor and boundary.match(lines[i]) and size >= target:
            spans.append((start, i))          # boundary line opens next chunk
            start = i + 1
        i += 1
    if start <= len(lines):
        spans.append((start, len(lines)))
    return spans


def _merge_to_budget(spans: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """Merge adjacent spans until at most `n` remain (whole-file coverage wins
    over the 300-400 line target when the call budget is small). Greedy: always
    merge the adjacent pair with the smallest combined size.

    Test: result length == min(len(spans), n); spans stay contiguous/ordered.
    """
    spans = list(spans)
    while len(spans) > n:
        best, best_sz = 0, None
        for k in range(len(spans) - 1):
            sz = spans[k + 1][1] - spans[k][0]
            if best_sz is None or sz < best_sz:
                best, best_sz = k, sz
        spans[best:best + 2] = [(spans[best][0], spans[best + 1][1])]
    return spans


def _numbered(lines: list[str], span: tuple[int, int]) -> str:
    s, e = span
    return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(s, e + 1))


def _extract_json_array(reply: str) -> list:
    """Lenient parse: whole reply, then fenced block, then first [...] span.
    Why: free-tier models wrap JSON in prose/fences. Test: each fallback path
    yields the same list for a valid embedded array; garbage raises ValueError.
    """
    for cand in (reply,
                 re.sub(r"^```[a-z]*\n|\n```\s*$", "", reply.strip()),
                 *(m.group(0) for m in [re.search(r"\[.*\]", reply, re.S)] if m)):
        try:
            out = json.loads(cand)
            if isinstance(out, list):
                return out
        except (json.JSONDecodeError, TypeError):
            continue
    raise ValueError("no JSON array in reply")


def _clean_findings(raw: list, span: tuple[int, int]) -> list[dict]:
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "medium")).lower()
        out.append({
            "principle": str(f.get("principle", "?"))[:8],
            "location": str(f.get("location", f"spec_flow_runner.py:{span[0]}"))[:120],
            "description": str(f.get("description", "")).strip()[:400],
            "severity": sev if sev in _SEVERITIES else "medium",
            "chunk": f"{span[0]}-{span[1]}",
        })
    return [f for f in out if f["description"]]


def _ask(prompt: str, system: str, step: str) -> str:
    chain = lb.chain_for("reviewer")
    return lb.ask(prompt, model=chain[0], fallbacks=chain[1:],
                  role="reviewer", step=step, system=system,
                  params={"temperature": 0.1, "max_tokens": 2500},
                  meta={"purpose": "audit-revision"})


def _fallback_top(findings: list[dict], limit: int = 10) -> str:
    """Deterministic local ranking used when the synthesis call itself fails:
    severity-major sort, first-seen dedup by (principle, description prefix).
    Why: the report must never be empty just because ONE call died.
    Test: two findings differing only in chunk collapse to one entry."""
    order = {s: i for i, s in enumerate(_SEVERITIES)}
    seen, top = set(), []
    for f in sorted(findings, key=lambda f: (order[f["severity"]], f["principle"])):
        key = (f["principle"], f["description"][:80].lower())
        if key in seen:
            continue
        seen.add(key)
        top.append(f"{len(top) + 1}. **[{f['principle']}/{f['severity']}]** "
                   f"{f['location']} — {f['description']}")
        if len(top) >= limit:
            break
    return "\n".join(top) if top else "_no findings collected_"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-llm-calls", type=int, default=15,
                    help="hard budget for LLM calls, synthesis included")
    ap.add_argument("--target", default=str(TARGET_DEFAULT),
                    help="file to review (default: the engine runner)")
    args = ap.parse_args(argv)
    budget = max(2, args.max_llm_calls)

    lb.configure_workers(dict(WORKERS_CFG))
    charter = CHARTER.read_text(encoding="utf-8")
    target = Path(args.target)
    lines = target.read_text(encoding="utf-8").splitlines()

    spans = _semantic_chunks(lines)
    spans = _merge_to_budget(spans, budget - 1)     # reserve 1 call for synthesis

    findings: list[dict] = []
    skipped: list[str] = []
    calls = 0
    for span in spans:
        if calls >= budget - 1:
            skipped.append(f"{span[0]}-{span[1]} (call budget exhausted)")
            continue
        prompt = (f"CHARTER:\n{charter}\n\n"
                  f"FILE: {target.name} lines {span[0]}-{span[1]} "
                  f"(of {len(lines)}), line-number prefixed:\n\n"
                  f"{_numbered(lines, span)}")
        calls += 1
        try:
            reply = _ask(prompt, _MAP_SYSTEM, step=f"chunk-{span[0]}")
            findings.extend(_clean_findings(_extract_json_array(reply), span))
        except Exception as exc:  # noqa: BLE001 — one dead chunk must not kill the revision
            skipped.append(f"{span[0]}-{span[1]} ({type(exc).__name__}: {str(exc)[:120]})")

    top_md, synth_note = "", ""
    if findings and calls < budget:
        calls += 1
        try:
            top_md = _ask("RAW FINDINGS:\n" + json.dumps(findings, indent=1),
                          _SYNTH_SYSTEM, step="synthesis").strip()
        except Exception as exc:  # noqa: BLE001
            synth_note = (f"synthesis call failed ({type(exc).__name__}); "
                          f"deterministic local ranking used instead")
    if not top_md:
        if not synth_note and findings:
            synth_note = "no synthesis budget left; deterministic local ranking used"
        top_md = _fallback_top(findings)

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    chain = lb.chain_for("reviewer")
    body = [
        f"# Engine revision report — {target.name}",
        "",
        f"- date: {now}",
        f"- charter: tests/audit/CHARTER.md",
        f"- model chain: {' -> '.join(chain)}",
        f"- llm calls used: {calls}/{budget}",
        f"- chunks reviewed: {len(spans) - len(skipped)}/{len(spans)}"
        + (f" (skipped: {'; '.join(skipped)})" if skipped else ""),
        f"- raw findings: {len(findings)}",
    ]
    if synth_note:
        body.append(f"- note: {synth_note}")
    body += ["", "## Top findings", "", top_md, "", "## Raw findings (appendix)", ""]
    for f in findings:
        body.append(f"- [{f['principle']}/{f['severity']}] {f['location']} "
                    f"(chunk {f['chunk']}) — {f['description']}")
    if not findings:
        body.append("_none_")
    REPORT.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"wrote {REPORT} — {len(findings)} finding(s), "
          f"{calls}/{budget} calls, {len(skipped)} chunk(s) skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
