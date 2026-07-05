#!/usr/bin/env python3
"""Universality battery generator (node F1, spec-IR rearchitecture plan).

Why: the plan's target form is UNIVERSAL software — new tasks every time, no
memory of past projects, the spec as the only source of truth. Sixteen live
runs of the same p6 case reward per-case tuning; a battery of small generated
specs across domains makes p6 stop being the only yardstick. A failure of ANY
battery spec in a live sweep is a ratchet investigation (honest red).

What: deterministically generates N SMALL case YAMLs (2-5 routes or 2-4 lib
capabilities, <= 3 modules each) from a seed, across different domains AND
different structure (route counts, method sets, required request fields,
media mixes, module ownership, scenario counts, service vs library product
kind). Structure comes from a fixed SHAPE DECK shuffled by the seed, so every
seed's battery spans the dimensions while pairing shapes with different
domains. Output files are JSON-bodied ``.yaml`` (JSON is a YAML subset), so
generation stays stdlib-only and byte-stable; ``tests/lib/run_cases.py``
consumes them unchanged via its ``tests/scenarios/*.yaml`` glob and the live
sweep goes through the EXISTING ``tests/run-detached.sh`` launcher — the
battery is data plus this thin driver, never a second harness.

HARD honesty rule: a generated spec describes DESIRED BEHAVIOUR only — no
seed_files, no blueprint, no code fences, no function bodies, no injections.
``lint_case_text`` enforces it at grep level and audit S20.4 exercises the
linter in both directions.

Test: ``tests/audit/test_universality_battery.py`` (Stage 20) — determinism
(S20.1), committed-set == generator (S20.2), validity/size/IR-surface
(S20.3), honesty + selector safety (S20.4), structural variety (S20.5),
existing-launcher + no cross-run memory (S20.6).

CLI:
    python3 tests/lib/battery_gen.py            # plan + live run command
    python3 tests/lib/battery_gen.py --write    # (re)write battery into tests/scenarios/
    python3 tests/lib/battery_gen.py --check    # exit 1 if committed set drifted
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

DEFAULT_SEED = 20260705
DEFAULT_COUNT = 8
FILE_PREFIX = "bat"
MANIFEST_NAME = "battery-manifest.json"
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
_FORMAT = "spec-flow battery v1"
# one process, all battery cases sequentially; run_cases mints a FRESH run dir
# and a FRESH HERMES_HOME temp dir per case — no memory crosses spec boundaries
_RUN_CMD = ("cd tests && ./run-detached.sh --case " + FILE_PREFIX +
            " --depth product --workers real --decomposer llm")

# ── domain pools (names/nouns vary; STRUCTURE comes from the shape decks) ────

_SERVICE_DOMAINS = [
    {"key": "bookmarks", "noun": "bookmark", "plural": "bookmarks",
     "fields": ["url", "label", "folder"], "page": "/about",
     "topic": "what this bookmark keeper is"},
    {"key": "tasks", "noun": "task", "plural": "tasks",
     "fields": ["title", "priority", "assignee"], "page": "/help",
     "topic": "how this task tracker is used"},
    {"key": "contacts", "noun": "contact", "plural": "contacts",
     "fields": ["name", "email", "phone"], "page": "/about",
     "topic": "what this contact book stores"},
    {"key": "recipes", "noun": "recipe", "plural": "recipes",
     "fields": ["name", "cuisine", "instructions"], "page": "/overview",
     "topic": "what this recipe box offers"},
    {"key": "readings", "noun": "reading", "plural": "readings",
     "fields": ["sensor", "value", "unit"], "page": "/about",
     "topic": "what this sensor reading log records"},
    {"key": "quotes", "noun": "quote", "plural": "quotes",
     "fields": ["quote", "speaker", "source"], "page": "/about",
     "topic": "what this quote collection is"},
    {"key": "tickets", "noun": "ticket", "plural": "tickets",
     "fields": ["subject", "reporter", "severity"], "page": "/help",
     "topic": "how this ticket desk works"},
    {"key": "events", "noun": "event", "plural": "events",
     "fields": ["title", "venue", "date"], "page": "/overview",
     "topic": "what this event board lists"},
]

_LIB_DOMAINS = [
    {"key": "textstats", "entry": "textlib", "caps": [
        {"name": "char_count", "sig": "char_count(text) -> int",
         "desc": "the number of characters in a string",
         "example": "char_count('abc') == 3"},
        {"name": "line_count", "sig": "line_count(text) -> int",
         "desc": "the number of newline-separated lines in a string",
         "example": "line_count('a\\nb') == 2"},
        {"name": "reverse_text", "sig": "reverse_text(text) -> str",
         "desc": "the string with its characters in reverse order",
         "example": "reverse_text('ab') == 'ba'"},
        {"name": "upper_text", "sig": "upper_text(text) -> str",
         "desc": "the string converted to upper case",
         "example": "upper_text('ab') == 'AB'"},
    ]},
    {"key": "listkit", "entry": "listlib", "caps": [
        {"name": "dedupe", "sig": "dedupe(items) -> list",
         "desc": "the list with duplicates removed, first occurrence order kept",
         "example": "dedupe([1, 1, 2]) == [1, 2]"},
        {"name": "flatten", "sig": "flatten(lists) -> list",
         "desc": "one level of nested lists flattened into a single list",
         "example": "flatten([[1], [2, 3]]) == [1, 2, 3]"},
        {"name": "chunk", "sig": "chunk(items, size) -> list",
         "desc": "the list split into consecutive pieces of the given size",
         "example": "chunk([1, 2, 3], 2) == [[1, 2], [3]]"},
        {"name": "first_of", "sig": "first_of(items, default) -> value",
         "desc": "the first element, or the default when the list is empty",
         "example": "first_of([], 9) == 9"},
    ]},
    {"key": "numkit", "entry": "numlib", "caps": [
        {"name": "add_all", "sig": "add_all(numbers) -> number",
         "desc": "the sum of the numbers",
         "example": "add_all([1, 2, 3]) == 6"},
        {"name": "max_of", "sig": "max_of(numbers) -> number",
         "desc": "the largest of the numbers",
         "example": "max_of([3, 1, 2]) == 3"},
        {"name": "mean_of", "sig": "mean_of(numbers) -> float",
         "desc": "the arithmetic mean of the numbers",
         "example": "mean_of([2, 4]) == 3.0"},
        {"name": "clamp", "sig": "clamp(value, low, high) -> number",
         "desc": "the value limited to the inclusive [low, high] range",
         "example": "clamp(7, 1, 5) == 5"},
    ]},
]

# ── shape decks: the structural dimensions, guaranteed to span per battery ───
# Each service shape fixes: variant (static page site vs file-backed store),
# html page presence (media mix), required-field count, extra route
# (count | clear | None) and list ordering. Route counts across the deck are
# {3, 4, 5}; method sets {GET}, {GET, POST}, {GET, POST, DELETE}; module
# counts {1, 2, 3}; scenario counts {2, 3}. The deck is SHUFFLED by the seed
# (shape-to-domain pairing varies), never thinned — variety holds by
# construction for every seed.
_SERVICE_SHAPES = [
    {"variant": "static", "html": True, "fields": 0, "extra": None, "order": None},
    {"variant": "store", "html": False, "fields": 1, "extra": None, "order": "newest"},
    {"variant": "store", "html": False, "fields": 2, "extra": "count", "order": "oldest"},
    {"variant": "store", "html": True, "fields": 1, "extra": None, "order": "newest"},
    {"variant": "store", "html": False, "fields": 3, "extra": "clear", "order": "newest"},
    {"variant": "store", "html": True, "fields": 2, "extra": "count", "order": "oldest"},
]
# Lib shapes vary capability count AND module ownership (one module per
# capability vs one shared implementation module) — module counts {3, 2}.
_LIB_SHAPES = [
    {"caps": 2, "layout": "module-per-cap"},
    {"caps": 3, "layout": "single-module"},
]

_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}
_HITL = {"approve_required": False, "worker_may_ask_human": True,
         "human_may_intervene": True}
_CAUSES = {"empty_delta": {"ladder": ["reject_empty", "rework", "escalate_tier"]},
           "task_too_large": {"ladder": ["split", "record"]}}
_EVALUATOR = {"tier": "strong", "temperature": 0.0, "votes": 1,
              "require_cite": True}

# Transport/model config shared by EVERY battery spec — mirrors the operator's
# live p6/p7 `workers:` block (Bifrost gateway, mimo leads, claude/sonnet
# subscription fallback; no paid API models). One constant so a retune is one
# edit + regenerate; the battery never varies model config as a dimension.
_WORKERS = {
    "backend": "openai",
    "base_url": "http://127.0.0.1:8080/v1",
    "claude_gateway": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model_map": {"claude/sonnet": "anthropic/claude-sonnet-4-6",
                      "sonnet": "anthropic/claude-sonnet-4-6"}},
    "retries": 6,
    "timeout": 75,
    "leaf_depth": 3,
    "max_children": 3,
    "max_decompose_calls": 40,
    "creator_ensemble": 2,
    "max_concurrent_llm_requests": 3,
    "provider_concurrency": {"claude": 1, "anthropic": 1},
    "provider_timeout": {"claude": 180, "anthropic": 180},
    "cycle_models": False,
    "quota_wait_s": 20,
    "quota_retries": 3,
    "fallback_models": ["claude/sonnet"],
    "tiers": {
        "weak": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"],
                 "temperature": 0.2},
        "medium": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"],
                   "temperature": 0.1},
        "strong": {"models": ["xiaomimimo/mimo-v2.5-pro", "claude/sonnet"],
                   "temperature": 0.0}},
    "complexity_to_tier": {"leaf_small": "weak", "leaf_big": "medium",
                           "branch": "strong"},
    "providers": [
        {"name": "xiaomimimo", "kind": "openai",
         "model_prefix": "xiaomimimo/", "requests_per_day": 1000},
        {"name": "claude-cli", "kind": "claude", "model_prefix": "claude/"}],
    "defaults": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet",
                            "xiaomimimo/mimo-v2.5-pro"]},
    "stages": {
        "decompose": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"]},
        "implement": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"]},
        "review": {"models": ["xiaomimimo/mimo-v2.5", "claude/sonnet"],
                   "params": {"reasoning": "off"}},
        "verify": {"models": ["xiaomimimo/mimo-v2.5-pro", "claude/sonnet"],
                   "params": {"reasoning": "off"}}},
}


def _base_case(name: str, goal: str, target: str, constitution: list,
               acceptance: dict) -> dict:
    """Why: every battery case shares the same conveyor knobs — one builder
    keeps key order (and therefore bytes) identical across specs.
    What: assembles the full case dict around the spec-specific text blocks.
    Test: audit S20.3 finds every required key in every generated spec."""
    return {
        "name": name,
        "node_engine": "fsm",
        "goal": goal,
        "target": target,
        "policy": _POLICY,
        "constitution": constitution,
        "hitl": _HITL,
        "doctor": {"enabled": True},
        "causes": _CAUSES,
        "evaluator": _EVALUATOR,
        "review": {"on_reject": "rework", "max_rework": 2},
        "integrate": {"pre_gate": True},
        "acceptance": acceptance,
        "workers": _WORKERS,
    }


def _service_case(name: str, dom: dict, shape: dict) -> tuple:
    """Why: services are the conveyor's main medium; each shape must state a
    FROZEN literal-path contract so the IR/decomposer has one source of truth.
    What: returns (case_dict, dims_dict) for a service spec; dims record the
    structural coordinates the manifest publishes and audit S20.3 binds to
    the spec text ("METHOD /path" appears verbatim in the constitution).
    Test: audit S20.3 (2-5 routes, IR method/media surface, literal paths),
    S20.5 (dims spans across the deck)."""
    plural, noun = dom["plural"], dom["noun"]
    routes = [{"method": "GET", "path": "/health", "media": "json"}]
    constitution = ['GET /health responds 200 with JSON {"status": "ok"}.']
    e2e = []
    if shape["variant"] == "static":
        modules = [f"src/{dom['key']}_site.py"]
        routes += [{"method": "GET", "path": dom["page"], "media": "html"},
                   {"method": "GET", "path": "/version", "media": "json"}]
        constitution += [
            f"GET {dom['page']} responds 200 with a server-rendered HTML page "
            f"(text/html) describing {dom['topic']}; standard library only, "
            f"no JavaScript frameworks.",
            'GET /version responds 200 with JSON {"version": "1.0"}.',
            f"Module ownership is FIXED: every handler lives in "
            f"{modules[0]}. The engine owns the WSGI entry point.",
        ]
        e2e = [f"GET {dom['page']} -> 200 HTML page containing a heading",
               'GET /version -> 200 JSON {"version": "1.0"}']
        goal = (f"A tiny read-only {noun} site over WSGI, standard library "
                f"only: a health endpoint, a server-rendered HTML page at "
                f"{dom['page']} describing {dom['topic']}, and a JSON "
                f"version endpoint. No storage, no forms, no JavaScript.")
        required, env = None, None
    else:
        required = dom["fields"][:shape["fields"]]
        env = f"{plural.upper()}_DB"
        modules = [f"src/{noun}_store.py", f"src/{plural}_api.py"]
        body = ", ".join(f'"{f}"' for f in required)
        order = shape["order"]
        routes += [{"method": "POST", "path": f"/{plural}", "media": "json"},
                   {"method": "GET", "path": f"/{plural}", "media": "json"}]
        constitution += [
            f'POST /{plural} takes a JSON body with required fields '
            f'{{{body}}} and responds 200 with JSON {{"id": <int>}}; a body '
            f'missing a required field responds 400.',
            f'GET /{plural} responds 200 with JSON {{"items": [...]}} where '
            f'each item carries "id" and the stored fields, {order} first.',
        ]
        sample = json.dumps({f: f"a {f}" for f in required})
        e2e = [f"POST /{plural} {sample} -> {{id}}; GET /{plural} -> items "
               f"contains that {noun}",
               f"two POSTs then GET /{plural} -> the {order} created "
               f"{noun} appears first"]
        if shape["extra"] == "count":
            routes.append({"method": "GET", "path": f"/{plural}/count",
                           "media": "json"})
            constitution.append(
                f'GET /{plural}/count responds 200 with JSON '
                f'{{"count": <int>}} — the number of stored {plural}.')
            e2e.append(f'after two POSTs, GET /{plural}/count -> '
                       f'{{"count": 2}}')
        elif shape["extra"] == "clear":
            routes.append({"method": "DELETE", "path": f"/{plural}",
                           "media": "json"})
            constitution.append(
                f'DELETE /{plural} removes every stored {noun} and responds '
                f'200 with JSON {{"cleared": true}}.')
            e2e.append(f"after a POST, DELETE /{plural} then GET /{plural} "
                       f"-> items is empty")
        if shape["html"]:
            modules.append(f"src/{dom['key']}_page.py")
            routes.append({"method": "GET", "path": dom["page"],
                           "media": "html"})
            constitution.append(
                f"GET {dom['page']} responds 200 with a server-rendered HTML "
                f"page (text/html) describing {dom['topic']}; standard "
                f"library only, no JavaScript frameworks.")
            if len(e2e) < 3:
                e2e.append(f"GET {dom['page']} -> 200 HTML page containing "
                           f"a heading")
        constitution += [
            f"Every test sets the {env} env var to a fresh temp path BEFORE "
            f"connecting; the store reads it per call.",
            f"Module ownership is FIXED: the {noun} store lives in "
            f"{modules[0]}; the HTTP handlers live in {modules[1]}"
            + (f"; the {dom['page']} page lives in {modules[2]}"
               if shape["html"] else "")
            + ". The engine owns the WSGI entry point.",
        ]
        goal = (f"A tiny {noun} keeper service over WSGI, standard library "
                f"only. It stores {plural} in a small file-backed store and "
                f"serves them over HTTP exactly as the frozen contract "
                f"states: create via POST /{plural}, list via GET /{plural} "
                f"({order} first)"
                + (f", plus an HTML page at {dom['page']}" if shape["html"]
                   else "") + ". No extras required.")
    constitution.append("Standard library only — no third-party packages.")
    target = ("Binary success: the assembled product boots in a fresh "
              "process and every endpoint frozen in the constitution "
              "answers exactly as stated; the acceptance suite is green.")
    acceptance = {"smoke": ["service starts and answers GET /health -> 200"],
                  "e2e": e2e}
    case = _base_case(name, goal, target, constitution, acceptance)
    dims = {"routes": routes, "route_count": len(routes),
            "methods": sorted({r["method"] for r in routes}),
            "media": sorted({r["media"] for r in routes}),
            "required_fields": required, "storage_env": env,
            "modules": modules, "module_count": len(modules),
            "scenario_count": len(e2e)}
    return case, dims


def _lib_case(name: str, dom: dict, shape: dict) -> tuple:
    """Why: p7 proved the conveyor is medium-agnostic; the battery must keep
    exercising the NON-WEB medium so universality is not web-only.
    What: returns (case_dict, dims_dict) for an importable-library spec —
    frozen signatures, layout-pinned modules, behaviour-by-example acceptance.
    Test: audit S20.3 (2-4 capabilities named in the text), S20.5 (both
    kinds present, module counts vary via layout)."""
    caps = dom["caps"][:shape["caps"]]
    entry = f"src/{dom['entry']}.py"
    names = ", ".join(f"`{c['name']}`" for c in caps)
    frozen = "; ".join(f"{c['sig']} is {c['desc']}" for c in caps)
    if shape["layout"] == "module-per-cap":
        modules = [f"src/{c['name']}.py" for c in caps] + [entry]
        layout = (f"Each capability lives in its OWN small module under "
                  f"src/ (one concern per module); the entry {entry} only "
                  f"re-exports them.")
    else:
        impl = f"src/{dom['entry']}_impl.py"
        modules = [impl, entry]
        layout = (f"All capabilities live in ONE module {impl}; the entry "
                  f"{entry} only re-exports them.")
    constitution = [
        f"This is a LIBRARY, not a service: standard library ONLY, no HTTP, "
        f"no server, no web framework. The public API is imported from the "
        f"package entry {entry}, which exposes {names}.",
        f"The public API is FROZEN: {frozen}. Accept exactly these "
        f"signatures — no extras required.",
        layout,
    ]
    goal = (f"A small, reusable Python library for {dom['key']} helpers. It "
            f"is IMPORTED by other code, not served over HTTP — there are "
            f"no endpoints and no web interface. The package entry {entry} "
            f"exposes {names}. Standard library only.")
    target = ("Binary success: importing the library and calling every "
              "exposed capability returns the results stated in the "
              "acceptance examples; the assembled library exposes each "
              "declared capability, callable.")
    imports = ", ".join(c["name"] for c in caps)
    acceptance = {
        "smoke": [f"the library imports: `from {dom['entry']} import "
                  f"{imports}` works"],
        "e2e": [c["example"] for c in caps]}
    case = _base_case(name, goal, target, constitution, acceptance)
    dims = {"capabilities": [c["name"] for c in caps],
            "layout": shape["layout"], "modules": modules,
            "module_count": len(modules), "scenario_count": len(caps)}
    return case, dims


def _render(case: dict, seed: int, index: int, domain: str,
            kind: str) -> str:
    """Why: generation must be stdlib-only AND byte-stable; JSON is a YAML
    subset, so yaml.safe_load (run_cases' loader) consumes it unchanged.
    What: provenance header comments + the case as pretty-printed JSON.
    Test: audit S20.1 (byte identity) and S20.3 (yaml.safe_load round-trip)."""
    header = (
        f"# GENERATED by tests/lib/battery_gen.py — DO NOT EDIT BY HAND.\n"
        f"# Universality battery spec (node F1): seed={seed} index={index} "
        f"domain={domain} kind={kind}.\n"
        f"# Regenerate: python3 tests/lib/battery_gen.py --write "
        f"(audit S20.2 pins the bytes).\n"
        f"# Body is JSON (a YAML subset) so generation stays stdlib-only "
        f"and byte-stable.\n")
    return header + json.dumps(case, indent=2, ensure_ascii=False) + "\n"


def generate(seed: int = DEFAULT_SEED, count: int = DEFAULT_COUNT) -> dict:
    """Why: the battery must be reproducible from ONE number so the committed
    set is auditable (same seed -> byte-identical files, S20.1/S20.2).
    What: returns {filename: text} — `count` spec files plus MANIFEST_NAME
    (the runner list). Kinds interleave deterministically (every 4th spec is
    a library); domains are seed-sampled without repetition; shape decks are
    seed-shuffled and cycled so structural variety holds for every seed.
    Test: audit S20.1-S20.6 run entirely over this function's output."""
    rng = random.Random(seed)
    kinds = ["lib" if i % 4 == 2 else "service" for i in range(count)]
    n_service, n_lib = kinds.count("service"), kinds.count("lib")
    if n_service > len(_SERVICE_DOMAINS) or n_lib > len(_LIB_DOMAINS):
        raise ValueError(f"count={count} exceeds the domain pools "
                         f"({len(_SERVICE_DOMAINS)} service, "
                         f"{len(_LIB_DOMAINS)} lib)")
    service_domains = rng.sample(_SERVICE_DOMAINS, n_service)
    lib_domains = rng.sample(_LIB_DOMAINS, n_lib)
    service_shapes = list(_SERVICE_SHAPES)
    lib_shapes = list(_LIB_SHAPES)
    rng.shuffle(service_shapes)
    rng.shuffle(lib_shapes)
    files, entries = {}, []
    si = li = 0
    for idx, kind in enumerate(kinds):
        if kind == "service":
            dom = service_domains[si]
            shape = service_shapes[si % len(service_shapes)]
            si += 1
        else:
            dom = lib_domains[li]
            shape = lib_shapes[li % len(lib_shapes)]
            li += 1
        name = f"{FILE_PREFIX}{idx + 1:02d}_{dom['key']}"
        build = _service_case if kind == "service" else _lib_case
        case, dims = build(name, dom, shape)
        fname = f"{name}.yaml"
        files[fname] = _render(case, seed, idx, dom["key"], kind)
        entries.append({"file": fname, "name": name, "kind": kind,
                        "domain": dom["key"], "dims": dims})
    manifest = {
        "format": _FORMAT,
        "seed": seed,
        "count": count,
        "generator": "tests/lib/battery_gen.py",
        "specs": entries,
        "run": {"detached": _RUN_CMD,
                "note": ("run_cases substring-matches --case against the "
                         "file stem, so '--case " + FILE_PREFIX + "' runs "
                         "the whole battery sequentially with a fresh run "
                         "dir + fresh HERMES_HOME per spec")},
    }
    files[MANIFEST_NAME] = json.dumps(manifest, indent=2,
                                      ensure_ascii=False) + "\n"
    return files


# grep-level honesty rules: token -> named finding. Total and crash-free by
# design (pure text scan, no parse) so tampering that breaks parsing still
# lands on a NAMED rule instead of an exception.
_LEAK_KEY_RE = re.compile(
    r'^\s*"?(seed_files|blueprint|solution|skeleton|injections)"?\s*:',
    re.MULTILINE)


def lint_case_text(text: str) -> list:
    """Why: the HARD honesty rule — a battery spec states desired behaviour
    only; any seeded answer or code fragment makes a green run a lie.
    What: returns named findings ([] = clean): code fences, function-body
    fragments ('def '), and answer-carrying keys (seed_files/blueprint/
    solution/skeleton/injections) at grep level.
    Test: audit S20.4 — [] on every generated spec, non-empty on each
    tampered known-answer input."""
    findings = []
    if "```" in text:
        findings.append("code-fence: spec must describe behaviour, not code")
    if "def " in text:
        findings.append("function-body: 'def ' fragment leaked into the spec")
    m = _LEAK_KEY_RE.search(text)
    if m:
        findings.append(f"answer-key: forbidden key '{m.group(1)}' — "
                        f"no seeded answers in a battery spec")
    return findings


def _write(files: dict) -> None:
    """Why: --write is the one sanctioned way to (re)materialise the battery;
    hand edits are drift and audit S20.2 reds on them.
    What: writes every generated file into tests/scenarios/ and removes
    stale bat*.yaml that the current set no longer produces.
    Test: after --write, `--check` exits 0 and audit S20.2 is green."""
    for stale in SCENARIOS_DIR.glob(f"{FILE_PREFIX}*.yaml"):
        if stale.name not in files:
            stale.unlink()
            print(f"removed stale {stale.name}")
    for fname, text in files.items():
        (SCENARIOS_DIR / fname).write_text(text, encoding="utf-8")
        print(f"wrote {fname}")


def _check(files: dict) -> int:
    """Why: a cheap drift probe for CI/operators without running pytest.
    What: byte-compares the generated set against tests/scenarios/; returns
    a process exit code (0 clean, 1 drift/missing).
    Test: touch a committed bat*.yaml -> --check reports it and exits 1."""
    rc = 0
    for fname, text in files.items():
        p = SCENARIOS_DIR / fname
        if not p.exists():
            print(f"MISSING {fname}")
            rc = 1
        elif p.read_text(encoding="utf-8") != text:
            print(f"DRIFT   {fname}")
            rc = 1
    if rc == 0:
        print(f"battery clean: {len(files) - 1} specs + manifest match "
              f"the generator")
    return rc


def main(argv: list) -> int:
    """Why: the thin driver — generate/verify the battery; the LIVE sweep is
    launched by the operator through the EXISTING tests/run-detached.sh.
    What: --write materialises, --check verifies, default prints the plan
    and the exact detached-run command.
    Test: `python3 tests/lib/battery_gen.py` prints one line per spec and
    the run command; exit code 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--write", action="store_true",
                    help="write the battery into tests/scenarios/")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed battery drifted")
    args = ap.parse_args(argv)
    files = generate(seed=args.seed, count=args.count)
    if args.check:
        return _check(files)
    if args.write:
        _write(files)
    manifest = json.loads(files[MANIFEST_NAME])
    for e in manifest["specs"]:
        d = e["dims"]
        shape = (f"routes={d['route_count']} methods={d['methods']} "
                 f"media={d['media']}" if e["kind"] == "service"
                 else f"caps={d['capabilities']} layout={d['layout']}")
        print(f"{e['file']:24s} {e['kind']:7s} {shape}")
    print(f"\nlive sweep (operator-launched): {manifest['run']['detached']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
