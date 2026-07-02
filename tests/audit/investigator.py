"""Layer-2 audit — the RUN INVESTIGATOR (LLM post-mortem pipeline).

Why: layer-1 audit (deterministic pytest rules in tests/audit) only catches
defect classes someone already distilled into code. A finished run still hides
NEW classes — contradictions between artifacts, half-executed engine decisions,
violations of the system's declared spirit. This investigator reads a run's
artifacts with three independent LLM lenses, adversarially cross-examines every
finding, and synthesises the survivors into (1) an evidence-backed report and
(2) CANDIDATE deterministic rules to be promoted into layer-1.

What: builds a compact dossier from <run_dir> (trace digest, spec heads,
product results, decomposition record, file inventory), fans it to three
auditor lenses (contracts / process / spirit), refutes each finding with a
separate adversarial call, then one pro-model synthesis call deduplicates and
ranks the survivors. Writes <run_dir>/investigation.md and
<run_dir>/candidate-rules.md. Every LLM call goes through the single door
tests/harness/llm_backend.ask() — no direct HTTP to any provider.

Test: run on a finished run dir with a small budget
(`python3 tests/audit/investigator.py tests/runs-out/<run> --max-llm-calls 12`)
and assert both output files exist, the stdout summary lists surviving
findings, and the total number of ask() calls never exceeds the budget.

CLI:
    python3 tests/audit/investigator.py <run_dir> [--max-llm-calls N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# tests/ on sys.path so `from harness import llm_backend` resolves the same
# way it does for the runner (tests/lib/run_cases.py).
_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from harness import llm_backend  # noqa: E402  (path bootstrap above)

# ── models (operator-pinned for this audit layer) ────────────────────────────
LEAD_MODEL = "xiaomimimo/mimo-v2.5"          # lenses + refutation
SYNTH_MODEL = "xiaomimimo/mimo-v2.5-pro"     # final synthesis
BACKUP_MODEL = "claude/sonnet"               # subscription fallback

# Mirrors the `workers:` block of tests/scenarios/p6_micro_notes.yaml — every
# provider rides Bifrost's unified OpenAI endpoint; claude/* maps through the
# same gateway (anthropic provider serialised by Bifrost, concurrency 1).
_BIFROST_URL = os.environ.get("SPEC_FLOW_LLM_BASE_URL",
                              "http://127.0.0.1:8080/v1")
WORKERS_CFG = {
    "backend": "openai",
    "base_url": _BIFROST_URL,
    "claude_gateway": {
        "base_url": _BIFROST_URL,
        "model_map": {
            "claude/sonnet": "anthropic/claude-sonnet-4-6",
            "sonnet": "anthropic/claude-sonnet-4-6",
        },
    },
    "retries": 3,
    "timeout": 180,
    "quota_retries": 0,
    "temperature": 0.1,
    "provider_concurrency": {"claude": 1, "anthropic": 1},
    "providers": [
        {"name": "xiaomimimo", "kind": "openai",
         "model_prefix": "xiaomimimo/", "requests_per_day": 1000},
        {"name": "claude-cli", "kind": "claude", "model_prefix": "claude/"},
    ],
    "defaults": {"models": [LEAD_MODEL, BACKUP_MODEL]},
}

# ── dossier size caps (compact by design — never dump whole artifacts) ───────
TRACE_MAX_LINES = 200
TRACE_DETAIL_CHARS = 260
SPEC_HEAD_LINES = 60
DECOMP_LINE_CHARS = 1500
DECOMP_TOTAL_CHARS = 4000
FINDING_EVIDENCE_CHARS = 600

FAIL_VERDICTS = {"FAIL", "REJECT", "AMEND", "NOT READY", "REJECTED"}


# ── data model ────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """One investigator finding plus its adversarial-check outcome.

    Why: the pipeline needs one carrier from lens output through refutation to
    synthesis, keeping the verbatim evidence attached the whole way.
    What: lens-provided fields + bookkeeping (lens name, refutation status).
    Test: construct with lens dict via from_llm(), assert defaults survive
    missing keys and status transitions land in survived/refuted/unchecked.
    """
    lens: str
    klass: str
    severity: str
    evidence: str
    reasoning: str
    candidate_rule: str
    status: str = "pending"        # pending | survived | refuted | unchecked
    refute_note: str = ""
    fid: int = field(default=0)

    @classmethod
    def from_llm(cls, lens: str, raw: dict) -> "Finding":
        def _s(key: str, alt: str = "") -> str:
            v = raw.get(key, raw.get(alt, ""))
            return str(v).strip() if v is not None else ""
        return cls(
            lens=lens,
            klass=_s("class", "klass") or "unclassified",
            severity=(_s("severity") or "minor").lower(),
            evidence=_s("evidence")[:FINDING_EVIDENCE_CHARS],
            reasoning=_s("reasoning"),
            candidate_rule=_s("candidate_rule", "rule"),
        )


class Budget:
    """Hard LLM-call budget shared by the whole pipeline.

    Why: a runaway audit must never spend more calls than the operator allowed
    (the free pool is a shared resource across runs).
    What: counts every attempted ask(); take() raises when exhausted.
    Test: Budget(2).take(); take(); third take() raises RuntimeError.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self, label: str) -> None:
        if self.used >= self.limit:
            raise RuntimeError(f"LLM budget exhausted before '{label}' "
                               f"({self.used}/{self.limit})")
        self.used += 1

    def left(self) -> int:
        return self.limit - self.used


# ── dossier assembly ──────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _trace_digest(run_dir: Path) -> str:
    """Milestone events (level<=1) + every FAIL/REJECT verdict with details,
    capped at TRACE_MAX_LINES lines."""
    lines: list[str] = []
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return "(no trace.jsonl)"
    for raw in _read(trace).splitlines():
        try:
            e = json.loads(raw)
        except (ValueError, TypeError):
            continue
        verdict = str(e.get("verdict", "") or "")
        milestone = int(e.get("level", 9) or 9) <= 1
        failing = verdict.upper() in FAIL_VERDICTS
        if not (milestone or failing):
            continue
        detail = str(e.get("detail", "") or "").replace("\n", " | ")
        if len(detail) > TRACE_DETAIL_CHARS:
            detail = detail[:TRACE_DETAIL_CHARS] + "…"
        lines.append(
            f"tick={e.get('tick')} phase={e.get('phase')} "
            f"task={e.get('task')} action={e.get('action')} "
            f"verdict={verdict or '-'} :: {detail}")
        if len(lines) >= TRACE_MAX_LINES:
            lines.append("… (trace digest truncated at cap)")
            break
    return "\n".join(lines) or "(empty trace)"


def _spec_heads(run_dir: Path) -> str:
    specs = sorted((run_dir / "workspace" / "specs").glob("*.md"))
    if not specs:
        return "(no specs)"
    out = []
    for p in specs:
        head = "\n".join(_read(p).splitlines()[:SPEC_HEAD_LINES])
        out.append(f"----- specs/{p.name} (first {SPEC_HEAD_LINES} lines) "
                   f"-----\n{head}")
    return "\n\n".join(out)


def _file_inventory(run_dir: Path) -> str:
    def _ls(sub: str) -> str:
        d = run_dir / "workspace" / sub
        names = sorted(p.name for p in d.glob("*.py")) if d.is_dir() else []
        return f"workspace/{sub}/: " + (", ".join(names) or "(none)")
    return _ls("src") + "\n" + _ls("tests")


def _decomp_digest(run_dir: Path) -> str:
    p = run_dir / "workspace" / ".spec-flow" / "decomp.jsonl"
    if not p.exists():
        return "(no decomp.jsonl)"
    out: list[str] = []
    total = 0
    for raw in _read(p).splitlines():
        line = raw[:DECOMP_LINE_CHARS] + ("…" if len(raw) > DECOMP_LINE_CHARS
                                          else "")
        total += len(line)
        out.append(line)
        if total > DECOMP_TOTAL_CHARS:
            out.append("… (decomp digest truncated at cap)")
            break
    return "\n".join(out)


def _meta_digest(run_dir: Path) -> str:
    p = run_dir / "meta.json"
    if not p.exists():
        return "(no meta.json)"
    try:
        meta = json.loads(_read(p))
    except (ValueError, TypeError):
        return "(unreadable meta.json)"
    scalars = {k: v for k, v in meta.items()
               if isinstance(v, (str, int, float, bool))}
    nested = [k for k, v in meta.items() if not isinstance(
        v, (str, int, float, bool))]
    body = json.dumps(scalars, ensure_ascii=False, indent=1)
    return body + "\nnested keys (contents omitted): " + ", ".join(nested)


def _inputs_digest(run_dir: Path) -> str:
    p = run_dir / "inputs.json"
    if not p.exists():
        return "(no inputs.json)"
    try:
        inputs = json.loads(_read(p))
    except (ValueError, TypeError):
        return "(unreadable inputs.json)"
    keep = {k: inputs[k] for k in ("goal", "target", "constitution")
            if k in inputs}
    return json.dumps(keep, ensure_ascii=False, indent=1)


def build_dossier(run_dir: Path) -> str:
    """Assemble the compact run dossier every lens receives.

    Why: lenses must reason over the SAME curated evidence — a dossier keeps
    calls comparable, reproducible and cheap (never the raw artifacts).
    What: inputs, meta, trace digest, decomposition record, spec heads,
    file inventory, product verdict — sizes capped by module constants.
    Test: on a finished run dir, assert every section header is present and
    the total size stays in the tens-of-KB range.
    """
    sections = [
        ("RUN INPUTS (goal / target / constitution)", _inputs_digest(run_dir)),
        ("META (how the run was launched)", _meta_digest(run_dir)),
        ("TRACE DIGEST (milestones + every FAIL/REJECT with detail)",
         _trace_digest(run_dir)),
        ("DECOMPOSITION RECORD (workspace/.spec-flow/decomp.jsonl, truncated)",
         _decomp_digest(run_dir)),
        ("SPEC HEADS (workspace/specs/*.md)", _spec_heads(run_dir)),
        ("ASSEMBLED FILE INVENTORY", _file_inventory(run_dir)),
        ("PRODUCT-RESULTS.md (final product verdict)",
         _read(run_dir / "workspace" / "PRODUCT-RESULTS.md")
         or "(no PRODUCT-RESULTS.md)"),
    ]
    return "\n\n".join(f"===== {title} =====\n{body}"
                       for title, body in sections)


# ── LLM plumbing ─────────────────────────────────────────────────────────────
def _extract_json(text: str):
    """Parse a JSON value out of a model reply.

    Why: reasoning models wrap JSON in <think> blocks, fences and prose; a
    dropped lens costs a third of the investigation, so parsing must be
    aggressive before giving up.
    What: strips think-blocks, tries fenced blocks and the raw text, then
    scans every '['/'{' position with raw_decode and returns the first value;
    an object carrying a findings-like list key is unwrapped to that list.
    Test: feed '<think>x [1</think>ok ```json [{"class":"a"}] ```' and assert
    the array comes back; feed prose-with-object and assert dict comes back.
    """
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    text = re.sub(r"<think>.*", " ", text, flags=re.S)   # unterminated block
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    dec = json.JSONDecoder()

    def _scan(cand: str):
        cand = cand.strip()
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            pass
        first_dict = None
        for m in re.finditer(r"[\[{]", cand):
            try:
                val, _ = dec.raw_decode(cand, m.start())
            except (ValueError, TypeError):
                continue
            if isinstance(val, list):        # arrays win: lens output shape
                return val
            if isinstance(val, dict) and first_dict is None:
                first_dict = val
        return first_dict

    for cand in fenced + [text]:
        val = _scan(cand)
        if val is None:
            continue
        # a lens sometimes wraps its array into {"findings": [...]}
        if isinstance(val, dict):
            for key in ("findings", "results", "items"):
                if isinstance(val.get(key), list):
                    return val[key]
        return val
    return None


_RAW_DIR: Path | None = None    # set by main(); raw replies for audit trail


def _ask(budget: Budget, prompt: str, *, model: str, step: str,
         system: str, max_tokens: int, models_used: list) -> str:
    """One budgeted call through the single LLM door with the pinned chain."""
    budget.take(step)
    reply = llm_backend.ask(
        prompt, model=model, role="auditor", step=step, system=system,
        fallbacks=(BACKUP_MODEL,) if model != BACKUP_MODEL else (),
        params={"temperature": 0.1, "max_tokens": max_tokens},
        meta={"purpose": "run-investigator", "mode": step})
    answered = dict(getattr(llm_backend, "last_call", {}) or {})
    models_used.append({"step": step,
                        "model": answered.get("model", model),
                        "backend": answered.get("backend", "?")})
    if _RAW_DIR is not None:     # keep the verbatim reply for the audit trail
        _RAW_DIR.mkdir(parents=True, exist_ok=True)
        seq = len(models_used)
        (_RAW_DIR / f"{seq:02d}-{step}.txt").write_text(
            reply, encoding="utf-8")
    return reply


# ── the three lenses ─────────────────────────────────────────────────────────
_SYSTEM = (
    "You are a forensic post-mortem investigator auditing one finished run of "
    "an autonomous spec-driven build system (goal -> decomposition tree -> "
    "per-leaf specs -> generated code+tests -> assembly -> acceptance). You "
    "receive a curated dossier of the run's artifacts. Work ONLY from the "
    "dossier: every claim must quote it verbatim. Be precise and skeptical; "
    "prefer few well-evidenced findings over many vague ones.")

_FINDING_FORMAT = (
    "Respond with a JSON array (no prose outside it). Each element:\n"
    "{\n"
    '  "class": "short defect-class name (generalised, not this run\'s '
    'specifics)",\n'
    '  "severity": "critical|major|minor",\n'
    '  "evidence": "verbatim quote(s) from the dossier proving it",\n'
    '  "reasoning": "why this is a real defect of the RUN/ENGINE, step by '
    'step",\n'
    '  "candidate_rule": "a deterministic check that would catch this CLASS '
    'in any future run (what artifacts to read, what condition to assert)"\n'
    "}\n"
    "Maximum 6 findings. If you find nothing for your lens, return [].\n"
    "Keep any preliminary reasoning SHORT — you MUST end your reply with the "
    "complete JSON array; a reply without it is discarded."
)

LENSES = {
    "lens_contracts": (
        "LENS: CONTRACTS AND SINGLE SOURCE OF TRUTH.\n"
        "Hunt for: (1) pairs of artifacts that MUST agree but contradict each "
        "other (spec vs spec, spec vs decomposition record, spec vs final "
        "file inventory, plan vs product verdict); (2) values that were "
        "derived/invented TWICE by different executors instead of flowing "
        "from one source (the same fact stated differently in two places); "
        "(3) artifacts referring to things that do not exist in the final "
        "product."),
    "lens_process": (
        "LENS: PROCESS INTEGRITY.\n"
        "Hunt for: (1) engine decisions executed only halfway — a structure "
        "was replaced/collapsed/rerouted but dependent text/artifacts were "
        "NOT updated to match; (2) failures that were detected and then left "
        "hanging without treatment (no remedy, or a remedy that did not "
        "address the diagnosed cause); (3) completed work that was thrown "
        "away or never made it into the assembled product."),
    "lens_spirit": (
        "LENS: SPIRIT OF THE SYSTEM.\n"
        "The system DECLARES these principles: atomicity (one leaf = one "
        "module, one concern); no guessing (anything not fixed by the goal/"
        "constitution must come from one authoritative decision, not be "
        "invented ad hoc); honest terminal state (the final verdict reflects "
        "reality, nothing is greenwashed); prose requirements are verifiable "
        "(every requirement maps to a real check of the exact surface it "
        "names). Hunt for violations OF SUBSTANCE — places where the run "
        "betrays a principle even though formal gates report green."),
}


def run_lenses(dossier: str, budget: Budget,
               models_used: list, notes: list) -> list[Finding]:
    """Fan the dossier to three independent lens calls.

    Why: three narrow perspectives find more than one broad prompt, and the
    later adversarial pass needs findings with distinct provenance.
    What: one ask() per lens; a failed lens is skipped with a note instead of
    killing the pipeline; findings get sequential ids.
    Test: stub ask() to return a fixed JSON array for one lens and raise for
    another; assert findings carry the lens name and a skip note is recorded.
    """
    findings: list[Finding] = []
    for step, lens_text in LENSES.items():
        prompt = (f"{lens_text}\n\n{_FINDING_FORMAT}\n\n"
                  f"===== DOSSIER =====\n{dossier}")
        try:
            # reasoning models spend most tokens thinking before the JSON —
            # a tight cap truncates the reply right before the array (seen
            # live: 4000 was exactly the failure mode for two lenses)
            reply = _ask(budget, prompt, model=LEAD_MODEL, step=step,
                         system=_SYSTEM, max_tokens=16000,
                         models_used=models_used)
        except Exception as exc:  # any provider failure: skip, keep going
            notes.append(f"{step}: SKIPPED — {type(exc).__name__}: {exc}")
            continue
        parsed = _extract_json(reply)
        if isinstance(parsed, dict):      # single finding without array wrap
            parsed = [parsed]
        if not isinstance(parsed, list):
            notes.append(f"{step}: reply not a JSON array — lens dropped")
            continue
        for raw in parsed:
            if not isinstance(raw, dict):
                continue
            f = Finding.from_llm(step, raw)
            if not (f.evidence or f.reasoning):   # vacuous stub: not a claim
                notes.append(f"{step}: dropped an empty finding stub")
                continue
            findings.append(f)
    for i, f in enumerate(findings, 1):
        f.fid = i
    return findings


# ── adversarial refutation ────────────────────────────────────────────────────
_REFUTE_SYSTEM = (
    "You are an adversarial reviewer. Your ONLY job is to try to REFUTE a "
    "colleague's finding about a finished run, using nothing but the dossier. "
    "A finding is refuted when the dossier contradicts it, when the quoted "
    "evidence does not actually appear in or support it, or when the claimed "
    "defect is explicitly expected/handled by the run. Absence of extra "
    "confirmation is NOT refutation. Be strict and honest.")


def refute(findings: list[Finding], dossier: str, budget: Budget,
           models_used: list, notes: list) -> None:
    """One adversarial call per finding; survivors keep status='survived'.

    Why: a single-model finding is a hypothesis, not evidence — only claims
    that withstand a dedicated refutation attempt reach the report.
    What: iterates findings by severity; one call is always reserved for
    synthesis, findings beyond the remaining budget become status='unchecked'.
    Test: stub ask() returning {"refuted": true} and assert the finding is
    dropped; with a 1-call budget assert findings become 'unchecked'.
    """
    sev_rank = {"critical": 0, "major": 1, "minor": 2}
    ordered = sorted(findings, key=lambda f: sev_rank.get(f.severity, 3))
    for f in ordered:
        if budget.left() <= 1:          # keep the synthesis call alive
            f.status = "unchecked"
            f.refute_note = "not adversarially checked (budget exhausted)"
            continue
        prompt = (
            "FINDING UNDER REVIEW:\n"
            f"- class: {f.klass}\n- severity: {f.severity}\n"
            f"- evidence: {f.evidence}\n- reasoning: {f.reasoning}\n\n"
            "Try to refute it strictly from the dossier below. Respond with "
            "ONLY a JSON object: {\"refuted\": true|false, \"why\": \"…\"}.\n\n"
            f"===== DOSSIER =====\n{dossier}")
        try:
            reply = _ask(budget, prompt, model=LEAD_MODEL, step="refute",
                         system=_REFUTE_SYSTEM, max_tokens=6000,
                         models_used=models_used)
        except Exception as exc:
            f.status = "unchecked"
            f.refute_note = (f"refutation call failed "
                             f"({type(exc).__name__}) — kept, unverified")
            notes.append(f"refute #{f.fid}: SKIPPED — {exc}")
            continue
        verdict = _extract_json(reply)
        if isinstance(verdict, dict) and "refuted" in verdict:
            f.status = "refuted" if verdict.get("refuted") else "survived"
            f.refute_note = str(verdict.get("why", ""))[:500]
        else:
            f.status = "unchecked"
            f.refute_note = "refuter reply unparsable — kept, unverified"


# ── synthesis ─────────────────────────────────────────────────────────────────
_SYNTH_SYSTEM = (
    "You are the lead investigator writing the final synthesis of a run "
    "post-mortem. You receive the findings that survived adversarial review. "
    "Deduplicate (merge findings describing the same underlying defect even "
    "if worded differently by different lenses), rank by severity and "
    "evidence strength, and give a one-paragraph overall verdict.")


def synthesise(survivors: list[Finding], budget: Budget,
               models_used: list, notes: list) -> dict:
    """One pro-model call that groups and ranks the surviving findings.

    Why: three lenses overlap; the report needs one deduplicated ranking, and
    only the strongest model is trusted with the final ordering.
    What: returns {"groups": [{"ids": [...], "title": str, "severity": str,
    "summary": str}], "overall": str}; on any failure falls back to a
    deterministic one-group-per-finding structure so reports always render.
    Test: stub ask() raising -> fallback groups equal survivor count; stub a
    valid JSON reply -> parsed groups returned as-is.
    """
    fallback = {
        "groups": [{"ids": [f.fid], "title": f.klass,
                    "severity": f.severity, "summary": f.reasoning[:300]}
                   for f in survivors],
        "overall": "(synthesis unavailable — ungrouped survivor list)",
    }
    if not survivors:
        return {"groups": [], "overall": "No findings survived."}
    listing = json.dumps(
        [{"id": f.fid, "lens": f.lens, "class": f.klass,
          "severity": f.severity, "evidence": f.evidence,
          "reasoning": f.reasoning} for f in survivors],
        ensure_ascii=False, indent=1)
    prompt = (
        "SURVIVING FINDINGS (post adversarial review):\n" + listing +
        "\n\nRespond with ONLY a JSON object:\n"
        "{\"groups\": [{\"ids\": [finding ids merged into this group], "
        "\"title\": \"defect title\", \"severity\": \"critical|major|minor\", "
        "\"summary\": \"2-4 sentences: the defect, its impact, the fix "
        "direction\"}], \"overall\": \"one paragraph run verdict\"}\n"
        "Order groups from most to least important.")
    try:
        reply = _ask(budget, prompt, model=SYNTH_MODEL, step="synthesis",
                     system=_SYNTH_SYSTEM, max_tokens=8000,
                     models_used=models_used)
    except Exception as exc:
        notes.append(f"synthesis: SKIPPED — {type(exc).__name__}: {exc}")
        return fallback
    parsed = _extract_json(reply)
    if isinstance(parsed, dict) and isinstance(parsed.get("groups"), list):
        return parsed
    notes.append("synthesis: reply unparsable — deterministic fallback")
    return fallback


# ── rendering ─────────────────────────────────────────────────────────────────
def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:48] or "rule"


def render_reports(run_dir: Path, findings: list[Finding], synth: dict,
                   budget: Budget, models_used: list, notes: list) -> None:
    """Write investigation.md and candidate-rules.md into the run dir.

    Why: the post-mortem must live NEXT to the run it explains, so a later
    reader (or the rule-distillation loop) finds evidence and candidates
    together with the artifacts they cite.
    What: deterministic markdown from Finding objects + the synthesis
    grouping; LLM text is quoted, never trusted to be the file structure.
    Test: call with two fake findings and a stub synth dict, assert both
    files exist and every finding id appears in investigation.md.
    """
    by_id = {f.fid: f for f in findings}
    survivors = [f for f in findings if f.status == "survived"]
    unchecked = [f for f in findings if f.status == "unchecked"]
    refuted = [f for f in findings if f.status == "refuted"]

    lines = ["# Run investigation (layer-2 audit post-mortem)", ""]
    lines += [f"- Run: `{run_dir.name}`",
              f"- LLM calls used: {budget.used}/{budget.limit}",
              f"- Findings: {len(findings)} raised, {len(survivors)} survived "
              f"adversarial review, {len(refuted)} refuted, "
              f"{len(unchecked)} unchecked",
              ""]
    lines += ["## Overall verdict", "", str(synth.get("overall", "")), ""]
    lines += ["## Surviving findings (grouped, ranked)", ""]
    for gi, g in enumerate(synth.get("groups", []), 1):
        ids = [i for i in (g.get("ids") or []) if i in by_id]
        members = [by_id[i] for i in ids]
        if members and all(m.status == "refuted" for m in members):
            continue
        lines.append(f"### {gi}. {g.get('title', 'untitled')} "
                     f"[{g.get('severity', '?')}]")
        lines.append("")
        lines.append(str(g.get("summary", "")))
        lines.append("")
        for m in members:
            lines.append(f"- **F{m.fid} / {m.lens} / {m.severity} / "
                         f"{m.status}** — {m.klass}")
            lines.append(f"  - evidence: > {m.evidence}")
            lines.append(f"  - reasoning: {m.reasoning}")
            if m.refute_note:
                lines.append(f"  - adversarial check: {m.refute_note}")
        lines.append("")
    if unchecked:
        lines += ["## Unchecked findings (kept, not adversarially verified)",
                  ""]
        for m in unchecked:
            lines.append(f"- **F{m.fid} / {m.lens} / {m.severity}** — "
                         f"{m.klass}: {m.reasoning} ({m.refute_note})")
        lines.append("")
    if refuted:
        lines += ["## Refuted findings (dropped)", ""]
        for m in refuted:
            lines.append(f"- F{m.fid} / {m.lens} — {m.klass}: "
                         f"refuted — {m.refute_note}")
        lines.append("")
    if notes:
        lines += ["## Pipeline notes (skipped/failed stages)", ""]
        lines += [f"- {n}" for n in notes] + [""]
    lines += ["## Models that answered", ""]
    lines += [f"- {m['step']}: {m['model']} (backend {m['backend']})"
              for m in models_used] + [""]
    (run_dir / "investigation.md").write_text("\n".join(lines),
                                              encoding="utf-8")

    rules = ["# Candidate deterministic audit rules (layer-2 → layer-1)", "",
             "Each candidate below was distilled from a finding that survived "
             "adversarial review. Promotion path: implement as a cheap "
             "offline pytest in `tests/audit/`, red on the defect class, "
             "then fix the engine (never the test).", ""]
    seen: set[str] = set()
    for m in survivors + unchecked:
        if not m.candidate_rule:
            continue
        key = m.klass.lower()
        if key in seen:
            continue
        seen.add(key)
        rules.append(f"## {m.klass} [{m.severity}]")
        rules.append("")
        rules.append(f"- **How to check in code:** {m.candidate_rule}")
        rules.append(f"- **Where:** `tests/audit/test_{_slug(m.klass)}.py`")
        rules.append(f"- **Distilled from:** F{m.fid} ({m.lens}, "
                     f"status={m.status})")
        rules.append("")
    if not seen:
        rules.append("(no surviving findings carried a candidate rule)")
    (run_dir / "candidate-rules.md").write_text("\n".join(rules),
                                                encoding="utf-8")


# ── entry point ───────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """CLI: build the dossier, run the 3-lens + refute + synthesis pipeline.

    Why: one command turns a finished run dir into an evidence-backed
    post-mortem and a list of promotable audit rules.
    What: parses args, hard-caps the LLM budget, writes the two reports and a
    short stdout summary; returns 0 on success, 2 on a non-run directory.
    Test: point at a dir without trace.jsonl -> rc 2; on a real run dir ->
    rc 0 and both report files exist.
    """
    parser = argparse.ArgumentParser(
        description="Layer-2 run investigator: LLM post-mortem over a "
                    "finished spec-flow run directory.")
    parser.add_argument("run_dir", help="finished run directory "
                        "(tests/runs-out/<stamp>__vNNN__<case>)")
    parser.add_argument("--max-llm-calls", type=int, default=12,
                        help="hard budget for ask() calls (default 12)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "trace.jsonl").exists():
        print(f"error: {run_dir} does not look like a run dir "
              "(no trace.jsonl)", file=sys.stderr)
        return 2
    # observability: the single door logs every call when this is set
    os.environ.setdefault("SPEC_FLOW_LLM_LOG",
                          str(run_dir / "investigator-llm-log.jsonl"))
    global _RAW_DIR
    _RAW_DIR = run_dir / "investigation-raw"
    llm_backend.configure_workers(WORKERS_CFG)

    budget = Budget(args.max_llm_calls)
    models_used: list = []
    notes: list = []

    dossier = build_dossier(run_dir)
    print(f"dossier: {len(dossier)} chars; budget: {budget.limit} calls")

    findings = run_lenses(dossier, budget, models_used, notes)
    print(f"lenses done: {len(findings)} raw findings "
          f"({budget.used}/{budget.limit} calls)")

    refute(findings, dossier, budget, models_used, notes)
    survivors = [f for f in findings if f.status == "survived"]
    unchecked = [f for f in findings if f.status == "unchecked"]
    print(f"adversarial review done: {len(survivors)} survived, "
          f"{len(unchecked)} unchecked ({budget.used}/{budget.limit} calls)")

    synth = synthesise(survivors + unchecked, budget, models_used, notes)
    render_reports(run_dir, findings, synth, budget, models_used, notes)

    print(f"\n=== investigation summary ({run_dir.name}) ===")
    print(f"LLM calls: {budget.used}/{budget.limit}")
    for f in findings:
        mark = {"survived": "OK ", "unchecked": "?? ",
                "refuted": "XX ", "pending": ".. "}[f.status]
        print(f"  {mark}F{f.fid} [{f.severity}] {f.lens}: {f.klass}")
    for n in notes:
        print(f"  note: {n}")
    print(f"reports: {run_dir / 'investigation.md'}")
    print(f"         {run_dir / 'candidate-rules.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
