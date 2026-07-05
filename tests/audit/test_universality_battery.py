"""Audit rules S20.1-S20.6 (node F1, spec-IR plan): the universality battery.

Plan 2026-07-04T00-45 ("Целевая форма"): the goal is UNIVERSAL software —
every run gets NEW tasks, no memory of past projects, the spec is the only
source of truth. Until now p6 (plus p4/p5/p7) was the only live yardstick:
16 runs of the SAME case reward per-case tuning, not universality. Node F1
adds a BATTERY of small generated specs across domains; the conveyor must
build ALL of them with a fresh workspace per spec, and a failure of ANY
battery spec is a ratchet investigation (honest red, never softened).

The generator (`tests/lib/battery_gen.py`) is deterministic engine-side
code: seed -> byte-identical spec set, stdlib only, no LLM in generation.
Each generated spec is a case YAML the EXISTING conveyor consumes unchanged
(tests/lib/run_cases.py globs tests/scenarios/*.yaml); live runs go through
the EXISTING detached launcher (tests/run-detached.sh) — the battery is
data plus a thin driver, never a second harness.

Rules (each a test below):
  * S20.1 DETERMINISM: same seed -> byte-identical file set; a different
    seed actually changes the output (the seed is live, not decorative).
  * S20.2 COMMITTED SET == GENERATOR: the battery checked into
    tests/scenarios/ byte-equals a regeneration from the committed
    manifest's seed/count — hand-editing a battery spec is drift, caught
    here; the set has >= 6 specs so p6 stops being the only yardstick.
  * S20.3 VALIDITY: every generated spec parses with the same loader
    run_cases uses, carries the required case keys, stays SMALL (2-5
    routes, <= 3 modules), and every declared route uses only the
    method/media surface the IR schema admits (spec_ir constants) with
    literal paths (no templates). The manifest dims are BOUND to the spec
    text ("METHOD /path" must appear verbatim in the spec).
  * S20.4 HONESTY (no leakage): no seed_files / blueprint / solution
    fragments / code fences / function bodies anywhere in a battery spec —
    the spec describes desired behaviour only. Also selector safety:
    `--case bat` must select exactly the battery.
  * S20.5 VARIETY: the default battery spans the structural dimensions
    (route counts, method sets, media mixes, required-field counts,
    module counts, scenario counts, product kinds service AND lib,
    distinct domains) — breadth, not one shape with renamed nouns.
  * S20.6 EXISTING LAUNCHER, NO MEMORY: the manifest's recorded live
    command uses tests/run-detached.sh, and no battery spec smuggles a
    cross-run project memory (memory.project.mode, when present, is
    'fresh').

Both directions (v151 lesson): the honesty/validity linter is exercised on
the LEGITIMATE battery (zero findings) AND on tampered known-answer inputs
(each named rule fires) — a gate audited only for misses can sink the
battery with one false positive.

Deterministic: pure generation in memory + committed files; no engine run,
no LLM, no network.
"""
from __future__ import annotations

import json
import pathlib
import sys

import yaml

_TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS_DIR / "lib"))
sys.path.insert(0, str(_TESTS_DIR.parent))

import battery_gen  # noqa: E402
import spec_ir  # noqa: E402

SCENARIOS_DIR = _TESTS_DIR / "scenarios"


def _gen(seed=None, count=None):
    """Why: one place to call the generator with battery defaults.
    What: returns {filename: text} including the manifest entry.
    Test: keys include battery_gen.MANIFEST_NAME and >= 6 spec files."""
    seed = battery_gen.DEFAULT_SEED if seed is None else seed
    count = battery_gen.DEFAULT_COUNT if count is None else count
    return battery_gen.generate(seed=seed, count=count)


def _specs(files):
    """Why: tests reason about spec files apart from the manifest.
    What: {filename: text} for *.yaml battery entries only.
    Test: every key starts with battery_gen.FILE_PREFIX and ends '.yaml'."""
    return {k: v for k, v in files.items() if k.endswith(".yaml")}


def _manifest(files):
    """Why: the manifest is the battery's runner list — tests parse it once.
    What: json-decoded manifest dict from the generated file set.
    Test: has 'seed', 'count', 'specs', 'run' keys."""
    return json.loads(files[battery_gen.MANIFEST_NAME])


# ── S20.1 determinism ────────────────────────────────────────────────────────

def test_s20_1_same_seed_same_bytes():
    a = _gen()
    b = _gen()
    assert a == b, "same seed must produce a byte-identical file set"


def test_s20_1_seed_is_live():
    a = _gen(seed=battery_gen.DEFAULT_SEED)
    b = _gen(seed=battery_gen.DEFAULT_SEED + 1)
    assert a != b, "a different seed must change the generated set"


# ── S20.2 committed battery == generator (drift guard) ───────────────────────

def test_s20_2_committed_manifest_exists_and_is_big_enough():
    p = SCENARIOS_DIR / battery_gen.MANIFEST_NAME
    assert p.exists(), "the battery manifest must be committed to tests/scenarios/"
    man = json.loads(p.read_text(encoding="utf-8"))
    assert man["count"] >= 6, "p6 must stop being the only yardstick: >= 6 battery specs"
    assert len(man["specs"]) == man["count"]


def test_s20_2_committed_files_match_regeneration():
    man = json.loads((SCENARIOS_DIR / battery_gen.MANIFEST_NAME)
                     .read_text(encoding="utf-8"))
    regen = battery_gen.generate(seed=man["seed"], count=man["count"])
    for fname, text in regen.items():
        on_disk = (SCENARIOS_DIR / fname).read_text(encoding="utf-8")
        assert on_disk == text, (
            f"{fname}: committed battery file drifted from the generator — "
            f"regenerate via battery_gen.py --write, never hand-edit")
    # nothing extra pretends to be battery output
    stray = {p.name for p in SCENARIOS_DIR.glob(f"{battery_gen.FILE_PREFIX}*.yaml")}
    assert stray == set(_specs(regen)), "stray/missing bat*.yaml next to the manifest"


# ── S20.3 validity: consumable by the existing conveyor, small, IR-surface ───

_REQUIRED_KEYS = ("name", "node_engine", "goal", "target", "policy",
                  "constitution", "hitl", "review", "integrate",
                  "acceptance", "workers")


def test_s20_3_every_spec_is_a_valid_small_case():
    files = _gen()
    man = _manifest(files)
    by_file = {e["file"]: e for e in man["specs"]}
    allowed_methods = {m.upper() for m in spec_ir._OA_METHODS}
    allowed_media = set(spec_ir._MIME)
    for fname, text in _specs(files).items():
        case = yaml.safe_load(text)  # same loader run_cases.py uses
        assert isinstance(case, dict), f"{fname}: not a mapping"
        for key in _REQUIRED_KEYS:
            assert key in case, f"{fname}: missing case key '{key}'"
        assert case["name"] == pathlib.Path(fname).stem, (
            f"{fname}: case name must equal the file stem (run-dir/selector unity)")
        assert isinstance(case["constitution"], list) and case["constitution"]
        assert case["integrate"].get("pre_gate") is True, (
            f"{fname}: the boot-gate is the honest floor — pre_gate must be on")
        acc = case["acceptance"]
        assert acc.get("smoke") and acc.get("e2e"), f"{fname}: empty acceptance"
        entry = by_file[fname]
        dims = entry["dims"]
        assert dims["module_count"] <= 3, f"{fname}: battery specs stay SMALL"
        if entry["kind"] == "service":
            routes = dims["routes"]
            assert 2 <= len(routes) <= 5, f"{fname}: 2-5 routes required"
            for r in routes:
                assert r["method"] in allowed_methods, f"{fname}: {r['method']}"
                assert r["media"] in allowed_media, f"{fname}: {r['media']}"
                assert "{" not in r["path"] and "}" not in r["path"], (
                    f"{fname}: literal paths only, no templates")
                # dims are BOUND to the spec text, not a parallel truth
                assert f"{r['method']} {r['path']}" in text, (
                    f"{fname}: route {r['method']} {r['path']} not stated in the spec")
        else:
            caps = dims["capabilities"]
            assert 2 <= len(caps) <= 4, f"{fname}: 2-4 lib capabilities"
            for cap in caps:
                assert cap in text, f"{fname}: capability {cap} not stated in the spec"


def test_s20_3_linter_green_on_legitimate_battery():
    for fname, text in _specs(_gen()).items():
        assert battery_gen.lint_case_text(text) == [], (
            f"{fname}: the linter must stay silent on a legitimate battery spec")


# ── S20.4 honesty: no leakage, selector safety ───────────────────────────────

_FORBIDDEN_KEYS = {"seed_files", "blueprint", "tree", "solution", "skeleton"}


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def test_s20_4_no_solution_leakage_in_specs():
    for fname, text in _specs(_gen()).items():
        assert "```" not in text, f"{fname}: code fence — spec must be behaviour only"
        assert "def " not in text, f"{fname}: function body fragment leaked"
        case = yaml.safe_load(text)
        for where, key, _val in _walk(case):
            assert key not in _FORBIDDEN_KEYS, (
                f"{fname}: forbidden key '{key}' at {where} — no seeded answers")
        assert "injections" not in case, (
            f"{fname}: battery specs carry no injections (breadth, not HITL depth)")


def test_s20_4_linter_reds_on_tampered_specs():
    base = next(iter(_specs(_gen()).values()))
    tampered = {
        "seed_files": base + "\nseed_files:\n  - src/app.py\n",
        "code_fence": base.replace("goal:", "goal: |\n  ```python\n  x = 1\n  ```\n#", 1),
        "def_body": base + "\n# def build_app(): return app\n",
    }
    for rule, text in tampered.items():
        findings = battery_gen.lint_case_text(text)
        assert findings, f"linter missed tampering class '{rule}'"


def test_s20_4_selector_prefix_is_exclusive():
    # `--case bat` (run_cases substring match) must select EXACTLY the battery
    for fname in _specs(_gen()):
        assert fname.startswith(battery_gen.FILE_PREFIX)
    for p in SCENARIOS_DIR.glob("*.yaml"):
        if not p.stem.startswith(battery_gen.FILE_PREFIX):
            assert battery_gen.FILE_PREFIX not in p.stem, (
                f"{p.name}: non-battery scenario stem contains "
                f"'{battery_gen.FILE_PREFIX}' — --case selector would leak")


# ── S20.5 structural variety ─────────────────────────────────────────────────

def test_s20_5_battery_spans_structural_dimensions():
    man = _manifest(_gen())
    entries = man["specs"]
    kinds = {e["kind"] for e in entries}
    assert kinds == {"service", "lib"}, "both product kinds must be present"
    services = [e for e in entries if e["kind"] == "service"]
    assert len({e["dims"]["route_count"] for e in services}) >= 2
    assert len({frozenset(e["dims"]["methods"]) for e in services}) >= 2
    media_mixes = {frozenset(e["dims"]["media"]) for e in services}
    assert any("html" in m for m in media_mixes) and any(
        "html" not in m for m in media_mixes), "json-only AND json+html mixes"
    assert len({len(e["dims"]["required_fields"]) for e in services
                if e["dims"].get("required_fields") is not None}) >= 2
    assert len({e["dims"]["module_count"] for e in entries}) >= 2
    assert len({e["dims"]["scenario_count"] for e in entries}) >= 2
    domains = [e["domain"] for e in entries]
    assert len(set(domains)) == len(domains), "domains must not repeat in one battery"
    assert len(set(domains)) >= 6


# ── S20.6 existing launcher, fresh workspace, no cross-run memory ────────────

def test_s20_6_live_command_uses_existing_detached_launcher():
    man = _manifest(_gen())
    cmd = man["run"]["detached"]
    assert "run-detached.sh" in cmd, "live sweeps go through the EXISTING launcher"
    assert f"--case {battery_gen.FILE_PREFIX}" in cmd
    assert "--depth product" in cmd and "--workers real" in cmd \
        and "--decomposer llm" in cmd


def test_s20_6_no_cross_run_project_memory():
    for fname, text in _specs(_gen()).items():
        case = yaml.safe_load(text)
        mem = case.get("memory") or {}
        proj = (mem.get("project") or {}).get("mode", "fresh")
        assert proj == "fresh", (
            f"{fname}: project memory must be fresh — universality means "
            f"no memory of past projects")
