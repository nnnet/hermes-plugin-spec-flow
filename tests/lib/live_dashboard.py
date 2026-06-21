#!/usr/bin/env python3
"""Live + offline run dashboard for spec-flow.

An interactive, auto-refreshing web view over a run folder under ``runs-out/``.
It reuses the PLUGIN's own reporting (``spec_flow_tools.build_run_report``) and
the run's real artifacts so you can qualitatively judge what the plugin did —
while a run is in progress (live) or after it finished (offline review).

What you get:
  * a clickable task TREE the plugin built (drill into children); per-node badges
    for the episodes that happened there (spike / clarify / contract / drift /
    HITL / review rework);
  * a per-node DETAIL panel: the level spec, its VERSION history (v1→vN with the
    revision finding and a diff), the generated code + test, the frozen contract,
    and the node's event timeline;
  * GLOBAL tabs: the plugin footprint report + methodology audit, the oracle
    verdict, the run summary, and the COMMITS journal (git-like version history);
  * a live FEED of what the agents are doing right now.

    python3 tests/live_dashboard.py [--run-dir <dir>] [--port 8088]

No --run-dir → follows the most recently modified run. Open http://localhost:8088
"""
from __future__ import annotations

import argparse
import html
import json
import os
import time as _time
import pathlib
import re
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import spec_flow_tools as T                                       # noqa: E402

OUT_DIR = ROOT / "tests" / "runs-out"
REFRESH_MS = 2000


# ── helpers ──────────────────────────────────────────────────────────────────
def _is_run_dir(p: pathlib.Path) -> bool:
    """A real run folder: a directory whose name carries the `__vNNN__`
    separator. Filters out side-folders like `_archive/` and `.run-counter`."""
    return p.is_dir() and "__" in p.name and not p.name.startswith((".", "_"))


def _latest_run() -> pathlib.Path | None:
    """The run the dashboard auto-follows when nothing is pinned.

    A LIVE run (no SUMMARY.md yet + a freshly-touched trace, see _run_active)
    always wins over any finished run —
    even one with a newer directory mtime. Without this, a dead older run whose
    workspace was touched after a newer run started (observed: a stale
    ``v018`` mtime-bumped at 16:38 outranked the actually-running ``v020`` from
    16:22) would be shown as 'live'. Version numbers also repeat across cases
    (two ``v020`` dirs — p4 and p6), so selection is by liveness + mtime, never
    by the ``vNNN`` label. Among equally-live (or all-dead) runs the newest
    mtime wins."""
    dirs = [p for p in OUT_DIR.iterdir() if _is_run_dir(p)] if OUT_DIR.exists() else []
    if not dirs:
        return None
    alive = [p for p in dirs if _run_active(p)]
    return max(alive or dirs, key=lambda p: p.stat().st_mtime)


def _resolve_run(name: str) -> pathlib.Path | None:
    """Validate a run-folder name from a request body and return its path,
    or None. The name must be a direct child of OUT_DIR and a real run dir —
    this is the traversal guard for select/delete (no '/', no '..')."""
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name:
        return None
    p = (OUT_DIR / name).resolve()
    if p.parent != OUT_DIR.resolve() or not _is_run_dir(p):
        return None
    return p


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    out: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _snake(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(s).lower()).strip("_")


_EPISODE_BADGE = {
    "spike": "🔬", "clarify": "❓", "contract": "📐", "drift": "🌀",
    "hitl": "✋", "review_fails": "⚖️",
}


# ── tree: from tree.json (finished) or llm-log (live, incremental) ────────────
def _tree_from_file(run_dir: pathlib.Path) -> dict | None:
    f = run_dir / "tree.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _tree_from_llm(llm: list[dict]) -> dict:
    """Reconstruct a partial tree from live decomposer outcomes (node->children)."""
    kids: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    seen_child: set[str] = set()
    for e in llm:
        if e.get("event") == "outcome" and e.get("role") == "decomposer":
            nid = e.get("node")
            ch = e.get("children") or []
            # spec-rework rounds log a second outcome with NO children —
            # an empty list must never erase the node's known children
            if ch or nid not in kids:
                kids[nid] = ch
            for c in ch:
                seen_child.add(c)
    roots = [n for n in kids if n not in seen_child] or (["L0"] if kids else [])

    def build(nid: str) -> dict:
        return {"id": nid, "children": [build(c) for c in kids.get(nid, [])]}

    if not roots:
        return {"id": "L0", "children": []}
    return build(roots[0])


def _tree_from_decomp(run_dir: pathlib.Path) -> dict | None:
    """Rebuild the tree from the engine's decomposition journal
    (workspace/.spec-flow/decomp.jsonl: node -> proposed children). This is the
    AUTHORITATIVE source — it covers nodes whose decomposition was RESTORED on a
    resume (those never re-ask the decomposer, so they leave no outcome in the
    llm-log; reconstructing from the llm-log alone then loses the real root and a
    late node like product_entry wrongly floats to the top)."""
    f = run_dir / "workspace" / ".spec-flow" / "decomp.jsonl"
    if not f.is_file():
        return None
    kids: dict[str, list[str]] = {}
    seen_child: set[str] = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        nid = rec.get("node")
        if not nid:
            continue
        ch = [c.get("id") for c in ((rec.get("payload") or {}).get("children") or [])
              if c.get("id")]
        if ch or nid not in kids:
            kids[nid] = ch
        seen_child.update(ch)
    if not kids:
        return None
    # L0 is the canonical goal root; otherwise the first node nobody parents
    roots = [n for n in kids if n not in seen_child]
    root = "L0" if "L0" in kids else (roots[0] if roots else None)
    if root is None:
        return None

    def build(nid: str, guard: frozenset) -> dict:
        if nid in guard:                       # cycle guard (never trust input)
            return {"id": nid, "children": []}
        g = guard | {nid}
        return {"id": nid, "children": [build(c, g) for c in kids.get(nid, [])]}

    return build(root, frozenset())


def _node_ids(tree: dict) -> set:
    out: set = set()

    def w(n):
        out.add(n.get("id"))
        for c in n.get("children") or []:
            w(c)
    w(tree)
    return out


# trace phases that prove a task id is a REAL lifecycle node (not structural
# noise) — used to attach late-injected nodes the decompose tree never knew.
_NODE_PHASES = {"implement", "review", "integrate", "lifecycle", "contract",
                "respec"}

# engine-internal nodes that appear in a run but were NEVER injected by a human
# — build machinery (checkpoint snapshots, the boot/assembly/contract gates, the
# built entry module). They get a ⚙️ badge so the tree distinguishes a TECHNICAL
# attachment from a late HUMAN requirement (web_ui, seller_directory) which keeps
# the 📌 pin.
_TECHNICAL_TASKS = {"checkpoint", "verify", "product_entry", "boot",
                    "boot_gate", "assembly", "integrate", "contract"}


def _attach_orphan_nodes(tree: dict, events: list) -> list:
    """Late requirements (web_ui, seller_directory) are added by the engine
    at root integrate, NOT as decomposer children — so the decompose-based
    live tree never contains them and they vanish from the left panel + the
    spec graph even while the status bar shows them being worked. Attach any
    node that ran (has lifecycle events) but is missing from the tree under
    the root, marked 'attached' so the UI badges it as a late injection.
    Returns the attached ids."""
    have = _node_ids(tree)
    order, evidence = [], set()
    for e in events:
        t = str(e.get("task") or "")
        if not t:
            continue
        base = t.split(":")[0]
        if not base or base in have or base == tree.get("id"):
            continue
        if str(e.get("phase")) in _NODE_PHASES or e.get("gate"):
            if base not in evidence:
                evidence.add(base)
                order.append(base)
    if not order:
        return []
    tree.setdefault("children", [])
    for base in order:
        node = {"id": base, "children": [], "attached": True}
        if base in _TECHNICAL_TASKS:
            node["technical"] = True   # engine machinery, not a human injection
        tree["children"].append(node)
    return order


def _collect_ids(node: dict, acc: set) -> None:
    acc.add(node.get("id"))
    for c in node.get("children", []) or []:
        _collect_ids(c, acc)


def _attach_pending_requirements(tree: dict, run_dir: "pathlib.Path | None") -> list:
    """Attach injected-but-not-yet-materialized requirements (a folder under
    hitl/requirements/<name>/) to the root as PENDING nodes, so a freshly
    injected web_ui shows in the tree/graph immediately instead of staying
    invisible until the engine places it at the next integrate. Skips names
    already present (materialized or event-attached). Returns attached ids."""
    if not run_dir:
        return []
    reqs = run_dir / "hitl" / "requirements"
    if not reqs.is_dir():
        return []
    have: set = set()
    _collect_ids(tree, have)
    added = []
    for d in sorted(reqs.iterdir()):
        if not d.is_dir():
            continue
        nid = d.name
        if nid in have:
            continue
        tree.setdefault("children", []).append(
            {"id": nid, "children": [], "attached": True, "pending": True})
        added.append(nid)
    return added


def _flatten(node: dict, depth: int, out: dict, parent: str | None) -> None:
    nid = node.get("id", "?")
    eps = [k for k in _EPISODE_BADGE if k in node]
    out[nid] = {
        "depth": depth, "parent": parent,
        "verdict": "branch" if node.get("children") else "leaf",
        "episodes": eps,
        "metrics": node.get("metrics", {}),
    }
    for c in node.get("children", []) or []:
        _flatten(c, depth + 1, out, nid)


def _tree_view(node: dict, meta: dict) -> dict:
    """Slim nested tree for the client (id, verdict, episodes, children)."""
    nid = node.get("id", "?")
    m = meta.get(nid, {})
    view = {
        "id": nid, "verdict": m.get("verdict", "leaf"),
        "episodes": m.get("episodes", []),
        "children": [_tree_view(c, meta) for c in node.get("children", []) or []],
    }
    if node.get("attached"):
        view["attached"] = True       # late-injected requirement node
    # engine machinery (checkpoint/gate/assembly/...) gets the ⚙️ badge whether
    # it was attached as an orphan OR appears as a real tree node — the name is
    # authoritative, so a checkpoint never renders as a 📌 human injection.
    if node.get("technical") or nid in _TECHNICAL_TASKS:
        view["technical"] = True      # engine machinery (⚙️, not a 📌 injection)
    if node.get("pending"):
        view["pending"] = True        # injected but not yet materialized
    return view


# ── per-node files + events ──────────────────────────────────────────────────
def _node_files(ws: pathlib.Path, nid: str) -> dict:
    # paths are returned relative to the RUN dir (ws is <run>/workspace), so the
    # /api/file endpoint (which resolves against the run dir) can read them
    sn = _snake(nid)
    spec = ws / "specs" / f"{nid}.md"
    versions = sorted("workspace/" + str(p.relative_to(ws))
                      for p in (ws / "specs").glob(f"{nid}.v*.md")) \
        if (ws / "specs").exists() else []
    code = ws / "src" / f"{sn}.py"
    test = ws / "tests" / f"test_{sn}.py"
    contracts = sorted("workspace/" + str(p.relative_to(ws))
                       for p in (ws / "contracts").glob(f"{nid}*")) \
        if (ws / "contracts").exists() else []
    return {
        "spec": f"workspace/specs/{nid}.md" if spec.exists() else None,
        "versions": versions,
        "code": f"workspace/src/{sn}.py" if code.exists() else None,
        "test": f"workspace/tests/test_{sn}.py" if test.exists() else None,
        "contract": contracts[0] if contracts else None,
    }


# which LLM role backs each trace phase (engine/deterministic steps use none);
# implement is an orchestra, so try the specialist roles in build order
_PHASE_ROLES = {
    "decompose": ("decomposer",),
    "review": ("reviewer",),
    "implement": ("implementer", "coder", "architect", "tester", "fixer"),
    "contract": ("implementer", "coder"),
    "verify": ("verifier",),
    "integrate": ("verifier",),
}


def _events_by_node(events: list[dict], llm: list[dict] | None = None) -> dict:
    # model used per (node, role), from call_start records (they carry the model;
    # call_ok/outcome drop it). Lets the events table show WHICH LLM/agent the
    # role used on each phase. Engine/deterministic steps stay blank.
    model_by: dict = {}
    for e in (llm or []):
        if e.get("event") == "call_start" and e.get("model"):
            key = (str(e.get("node") or "").split(":")[0], str(e.get("role") or ""))
            model_by[key] = str(e.get("model"))

    def _model(nid: str, e: dict) -> str:
        if str(e.get("profile") or "") == "engine":
            return ""                      # deterministic engine step — no LLM
        for role in _PHASE_ROLES.get(str(e.get("phase") or ""), ()):  # noqa
            m = model_by.get((nid, role))
            if m:
                return m
        return ""

    idx: dict[str, list[dict]] = {}
    for e in events:
        task = str(e.get("task") or "")
        nid = task.split(":")[0]
        if not nid:
            continue
        idx.setdefault(nid, []).append({
            "tick": e.get("tick"), "phase": e.get("phase"), "profile": e.get("profile"),
            "skill": e.get("skill"), "action": e.get("action"), "gate": e.get("gate"),
            "verdict": e.get("verdict"), "detail": e.get("detail"),
            "model": _model(nid, e),
        })
    return idx


# ── markdown -> html (headings, tables, bullets, code, hr) ────────────────────
def _md_to_html(md: str) -> str:
    lines, out, i, n = md.splitlines(), [], 0, len(md.splitlines())
    while i < n:
        ln = lines[i]
        if ln.startswith("```"):
            lang = ln[3:].strip().lower()
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            raw = "\n".join(buf)
            if lang == "mermaid":
                # client renders this with mermaid.js; un-double-escape & first so
                # labels show "&" not "&amp;", keep <br/> as a real label break
                out.append('<pre class="mermaid">'
                           + html.escape(raw.replace("&amp;", "&")) + "</pre>")
            else:
                out.append("<pre class=code>" + html.escape(raw) + "</pre>")
            continue
        if re.match(r"^\|.*\|\s*$", ln):
            tbl = []
            while i < n and re.match(r"^\|.*\|\s*$", lines[i]):
                tbl.append(lines[i])
                i += 1
            out.append(_table_html(tbl))
            continue
        if ln.startswith("#"):
            lvl = min(len(ln) - len(ln.lstrip("#")), 4)
            out.append(f"<h{lvl}>{_inline(ln.lstrip('# ').strip())}</h{lvl}>")
        elif ln.strip() in ("---", "***"):
            out.append("<hr>")
        elif ln.startswith(("- ", "* ")):
            items = []
            while i < n and lines[i].startswith(("- ", "* ")):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    return "\n".join(out)


def _table_html(rows: list[str]) -> str:
    def cells(r):
        return [c.strip() for c in r.strip().strip("|").split("|")]
    body = [r for r in rows if not re.match(r"^\|[\s:|-]+\|\s*$", r)]
    if not body:
        return ""
    head, *rest = body
    out = ["<table><thead><tr>"] + [f"<th>{_inline(c)}</th>" for c in cells(head)] + \
          ["</tr></thead><tbody>"]
    for r in rest:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(r)) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


def _esc(s: object, limit: int = 0) -> str:
    """Single HTML-escape helper for server-built fragments. limit>0 truncates
    (used for compact flow-chart labels); limit==0 keeps the full string."""
    out = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out[:limit] if limit else out


# ── starting inputs + service info (readable "what we start from") ───────────
def _kv_table(d: dict) -> str:
    rows = [f"| {html.escape(str(k))} | {html.escape(str(v))} |" for k, v in d.items()]
    return "| параметр | значение |\n|---|---|\n" + "\n".join(rows) if rows else ""


# Spec-pipeline stage names (verbs) ↔ internal worker role keys (-er nouns).
_STAGE2ROLE = {"decompose": "decomposer", "implement": "implementer",
               "review": "reviewer", "verify": "verifier"}
# RU stage labels for the map (the four STAGES of the spec pipeline).
_STAGE_RU = {"decomposer": "декомпозиция", "implementer": "реализация",
             "reviewer": "ревью", "verifier": "верификация"}
# steps that are SPECIALISTS within a stage (vs an engine step that is its own
# stage, e.g. amend-route, performed by a role)
_ORCHESTRA_STEPS = {"architect", "coder", "tester", "fixer"}


def _normalise_workers_block(workers) -> dict:
    """Flatten a raw ``workers`` block to {internal_role: cfg}.

    Accepts both the legacy flat shape (``workers.decomposer`` …) and the
    ``workers.stages`` grouping with verb names (``decompose`` …), so the
    dashboard renders either identically. A flat key wins over a stage of the
    same meaning."""
    if not isinstance(workers, dict):
        return {}
    out: dict = {}
    stages = workers.get("stages")
    if isinstance(stages, dict):
        for name, cfg in stages.items():
            out[_STAGE2ROLE.get(name, name)] = cfg
    for k, v in workers.items():
        if k == "stages":
            continue
        out[k] = v          # explicit flat key overrides the stage
    return out


def _team_card_md(run_dir: pathlib.Path, llm: list[dict]) -> list[str]:
    """Render the team card for a role whose executor is a TEAM (orchestra).

    Why: a role can be a single worker or a team of specialists; when it is a
    team the operator must see WHO is in it — each specialist, its function
    (role), where it runs (provider) and on what model/params — so the run is
    not an opaque "implementer". A single-worker run shows no card.

    What: returns markdown lines (a `## …` header + a table) listing each
    specialist → role → provider → model → params. The team config is read
    DEFENSIVELY from two sources, in order: (1) the run's persisted config
    (`meta.json` / `inputs.json` ``workers.<role>.team``, accepting both the bare
    ``[{role}…]`` list and the richer ``team: {specialists: [{role, provider,
    model, params}…]}`` shape); (2) if the config is absent, the team is
    reconstructed from the llm-log (``orchestra_start.steps`` for the roster, the
    per-step ``model`` from the orchestra ``call_start`` records, provider
    defaulting to "local"). Returns [] when no team is defined (solo run).

    Test: a case whose ``workers.implementer.team.specialists`` lists role +
    provider + model + params yields a card with those columns; a run with no
    team config but an orchestra llm-log yields a card derived from the log;
    a solo run yields [].
    """
    def _load(name: str) -> dict:
        f = run_dir / name
        if not f.exists():
            return {}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    workers = _normalise_workers_block(
        _load("meta.json").get("workers") or _load("inputs.json").get("workers"))
    # per-step models actually seen in the log (fills config gaps / log-only mode)
    log_models: dict = {}
    for s in (_orchestra_sequences(llm) or {}).values():
        for st in s:
            if st.get("step") and st.get("model"):
                log_models.setdefault(str(st["step"]), st["model"])

    teams: list[tuple[str, list]] = []          # (role_owner, [specialist dicts])
    if isinstance(workers, dict):
        for owner, cfg in workers.items():
            if not isinstance(cfg, dict):
                continue
            team = cfg.get("team")
            specs = None
            if isinstance(team, dict):
                specs = team.get("specialists")
            elif isinstance(team, list):
                specs = team
            if isinstance(specs, list) and specs:
                norm = [s if isinstance(s, dict) else {"role": s} for s in specs]
                teams.append((str(owner), norm))
    if not teams:
        # no persisted config — reconstruct the roster from the llm-log
        roster: list = []
        for e in llm:
            if e.get("event") == "orchestra_start":
                for r in (e.get("steps") or []):
                    if r not in roster:
                        roster.append(r)
        if not roster:
            roster = list(log_models.keys())
        if roster:
            teams.append(("implementer", [{"role": r} for r in roster]))

    if not teams:
        return []

    role_ru = {"implementer": "исполнитель", "decomposer": "декомпозитор",
               "reviewer": "ревьюер", "verifier": "верификатор"}
    out = ["## 👥 Команда (специалисты роли)"]
    for owner, specs in teams:
        out.append(f"**Роль-владелец:** {role_ru.get(owner, owner)}")
        rows = ["| специалист | роль (функция) | провайдер | модель | параметры |",
                "|---|---|---|---|---|"]
        for s in specs:
            role = s.get("role") or s.get("specialist") or "—"
            provider = s.get("provider") or "local"
            model = s.get("model") or log_models.get(str(role)) or "—"
            params = s.get("params") or {}
            ptxt = ", ".join(f"{k}={v}" for k, v in params.items()) \
                if isinstance(params, dict) and params else "—"
            rows.append(f"| {role} | {role} | {provider} | {model} | {ptxt} |")
        out.append("\n".join(rows))
    return out


def _inputs_md(run_dir: pathlib.Path, llm: list[dict] | None = None) -> str:
    ws = run_dir / "workspace"
    inp, meta = {}, {}
    if (run_dir / "inputs.json").exists():
        try:
            inp = json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if (run_dir / "meta.json").exists():
        try:
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    out: list[str] = []
    if inp.get("goal"):
        out += ["## 🎯 Цель проекта", inp["goal"]]
    if inp.get("target"):
        out += ["## 📏 Измеримая цель", f"`{inp['target']}`"]
    if inp.get("imprecise") or inp.get("resolved"):
        out += ["## 📝 Постановка"]
        if inp.get("imprecise"):
            out.append(f"- **размытая:** {inp['imprecise']}")
        if inp.get("resolved"):
            out.append(f"- **уточнённая:** {inp['resolved']}")
    if inp.get("constitution"):
        out += ["## 📜 Конституция (неизменные правила)"]
        out += [f"- {c}" for c in inp["constitution"]]
    if isinstance(inp.get("policy"), dict):
        out += ["## 🛂 Политика (policy_gate)", _kv_table(inp["policy"])]
    if inp.get("acceptance") is not None:
        out += ["## ✅ Критерии приёмки"]
        acc = inp["acceptance"]
        if isinstance(acc, dict):
            for k, v in acc.items():
                if isinstance(v, list):
                    out.append(f"- **{k}:**")
                    out += [f"  - {x}" for x in v]
                else:
                    out.append(f"- **{k}:** {v}")
        elif isinstance(acc, list):
            out += [f"- {x}" for x in acc]
        else:
            out.append(str(acc))
    if isinstance(inp.get("oracle"), dict):
        out += ["## 🔎 С чем сверяем (oracle — эталон проверки)"]
        orc = inp["oracle"]
        flat = {k: v for k, v in orc.items() if not isinstance(v, (list, dict))}
        if flat:
            out.append(_kv_table(flat))
        for k, v in orc.items():
            if isinstance(v, list):
                out.append(f"- **{k}:** {', '.join(map(str, v))}")
            elif isinstance(v, dict):
                out.append(f"- **{k}:** " + ", ".join(f"{a}={b}" for a, b in v.items()))

    # service / runtime info
    git = (ws / ".git").exists()
    commits = (ws / "COMMITS.md")
    n_commits = (sum(1 for ln in commits.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if ln.strip().startswith(("- ", "* ", "|")))
                 if commits.exists() else 0)
    svc = {
        "кейс": meta.get("case", run_dir.name),
        "глубина": meta.get("depth", "—"),
        "декомпозитор": meta.get("decomposer", "—"),
        "исполнитель": meta.get("implementer", "—"),
        "модель": meta.get("model", "—"),
        "движок узла (FSM/inline)": meta.get("node_engine", "—"),
        "шлюз LLM": meta.get("gateway", "—"),
        "бэкенд LLM": meta.get("backend", "claude"),
        "воркспейс": f"{run_dir.name}/workspace",
        "git-репозиторий": "да (.git)" if git else ("журнал COMMITS.md" if commits.exists() else "—"),
        "коммитов/версий": n_commits,
        "COST.md": "есть" if (run_dir / "COST.md").exists() else "—",
    }
    out += ["## ⚙️ Служебная информация", _kv_table(svc)]
    # role -> model map: which model each worker role actually ran on
    # (the workers block resolves it per role; meta.json records it)
    wm = meta.get("worker_models") or {}
    workers_norm = _normalise_workers_block(meta.get("workers") or {})
    if isinstance(wm, dict) and wm:
        rows: dict = {}
        for role, model in wm.items():
            label = _STAGE_RU.get(role, role)
            cfg = workers_norm.get(role)
            team = cfg.get("team") if isinstance(cfg, dict) else None
            specs = (team.get("specialists") if isinstance(team, dict)
                     else team if isinstance(team, list) else None)
            if isinstance(specs, list) and specs:
                # this stage runs a TEAM — show each specialist's provider+model,
                # not one misleading model (the implement stage is an orchestra)
                parts = []
                for s in specs:
                    if not isinstance(s, dict):
                        parts.append(str(s)); continue
                    r = s.get("role", "—")
                    prov = s.get("provider") or "local"
                    m = s.get("model") or "—"
                    parts.append(f"{r}={prov}:{m}" if prov != "local" else f"{r}={m}")
                rows[f"{label} (оркестр)"] = "; ".join(parts)
            else:
                rows[label] = model
        out += ["## 🧠 Карта: этап → модель", _kv_table(rows)]
    # team card: when a role's executor is a team, list its specialists. A
    # single-worker run adds nothing here.
    out += _team_card_md(run_dir, llm or [])
    return "\n\n".join(out) if out else "_исходные данные не записаны_"


def _lane_map(tree: dict | None) -> dict:
    """node id -> its L1 ancestor id (the lane). Root maps to itself."""
    lanes: dict = {}
    if not tree:
        return lanes
    lanes[tree.get("id", "L0")] = tree.get("id", "L0")

    def walk(n, lane):
        lanes[n.get("id")] = lane
        for c in n.get("children") or []:
            walk(c, lane)

    for child in tree.get("children") or []:
        walk(child, child.get("id"))
    return lanes


def _flow_mermaid(events: list[dict], tree: dict | None = None) -> str | None:
    """Execution-flow graph (mermaid). With a tree available the
    milestones are grouped into LANES (one subgraph per L1 branch) and
    chained within their lane — under parallel children the branches sit
    side by side instead of interleaving into one false chain. Episode
    phases are coloured so the loops stand out."""
    miles = [e for e in events if int(e.get("level") or 2) <= 1]
    if not miles:
        return None

    def clean(s: str) -> str:
        return re.sub(r'["\[\]|{}<>]', " ", str(s))[:46]

    ep_phase = {"research", "drift", "respec", "hitl"}
    lanes = _lane_map(tree)
    root_id = (tree or {}).get("id", "L0")
    # BT (bottom-to-top): the run STARTS at the bottom, the newest milestone
    # is on top — matches reading order while the run is live. Arrows still
    # point old -> new.
    lines = ["flowchart BT"]
    body: dict = {}        # lane -> [mermaid lines]
    prev_in: dict = {}     # lane -> previous node id (chain WITHIN a lane)
    order: list = []       # lanes in first-appearance order
    for idx, e in enumerate(miles):
        nid = f"s{idx}"
        task = str(e.get("task") or "")
        base = task.split(":")[0]
        lane = lanes.get(base, root_id) if lanes else root_id
        if lane not in body:
            body[lane] = []
            order.append(lane)
        node = clean(task or e.get("phase") or "")
        act = clean(e.get("action") or "")
        body[lane].append(f'    {nid}["{e.get("phase")} · {node}<br/>{act}"]')
        if str(e.get("verdict")) in {"REJECT", "FAIL", "ERROR"}:
            body[lane].append(f"    style {nid} fill:var(--err-bg),stroke:var(--err)")
        elif str(e.get("phase")) in ep_phase or e.get("gate") in ("clarify", "drift", "hitl"):
            body[lane].append(f"    style {nid} fill:var(--warn-bg),stroke:var(--warn)")
        prev = prev_in.get(lane)
        if prev is not None:
            edge = clean(e.get("verdict") or e.get("gate") or "")
            body[lane].append(f"    {prev} -->|{edge}| {nid}" if edge
                              else f"    {prev} --> {nid}")
        prev_in[lane] = nid
    if len(order) > 1:
        for lane in order:
            title = clean(lane)
            lines.append(f'  subgraph lane_{re.sub(r"\W", "_", str(lane))}'
                         f'["{title}"]')
            lines.append("    direction BT")
            lines += body[lane]
            lines.append("  end")
    else:
        for lane in order:
            lines += [ln[2:] for ln in body[lane]]
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def _flow_timeaxis(events: list[dict], tree: dict | None = None) -> str | None:
    """Same execution-flow FORM as `_flow_mermaid` (one column per L1-branch
    lane; coloured milestone boxes "phase · node / action" chained per lane),
    but laid on a VERTICAL TIME AXIS: every box sits at its real timestamp on a
    shared time ruler. Returns an HTML fragment (absolute-positioned)."""
    miles = [e for e in events
             if int(e.get("level") or 2) <= 1
             and isinstance(e.get("t"), (int, float))]
    if not miles:
        return None

    # checkpoints are NOT a flow of their own — they are run-wide markers. Pull
    # them out of the lane boxes and render each as a ★ on the time axis + a
    # dashed horizontal line across the whole chart (below).
    def _is_cp(e: dict) -> bool:
        return str(e.get("task") or "").split(":")[0] == "checkpoint"

    cps = [e for e in miles if _is_cp(e)]
    miles = [e for e in miles if not _is_cp(e)]
    if not miles:
        return None        # nothing but checkpoints -> no flow to chart


    lanes_map = _lane_map(tree)
    root_id = (tree or {}).get("id", "L0")
    order: list = []
    lane_of: list = []
    for e in miles:
        base = str(e.get("task") or "").split(":")[0]
        lane = lanes_map.get(base, root_id) if lanes_map else root_id
        lane_of.append(lane)
        if lane not in order:
            order.append(lane)
    lane_x = {lane: i for i, lane in enumerate(order)}

    # Group milestones by whole-second timestamp: each second is ONE row, so
    # parallel work in different lanes at the same instant shares a row (aligned
    # horizontally). Rows go NEWEST-FIRST (top = later time). Within a row a
    # lane's events STACK, so two events of the same lane never overlap.
    secs: dict = {}
    for e, lane in zip(miles, lane_of):
        secs.setdefault(int(float(e["t"])), []).append((e, lane))
    # give every checkpoint second a (possibly empty) row slot so it sits at its
    # real time on the axis even when no milestone shares that second.
    cp_secs = {int(float(e["t"])) for e in cps if isinstance(e.get("t"),
                                                             (int, float))}
    for s in cp_secs:
        secs.setdefault(s, [])

    AXIS, LANEW, PADT, BOXH, RGAP, CPH = 78, 250, 22, 56, 8, 18

    def hms(t) -> str:
        return _time.strftime("%H:%M:%S", _time.localtime(float(t)))

    rows = sorted(secs.keys(), reverse=True)        # newest second at the top
    row_top, row_h, by_lane = {}, {}, {}
    y = PADT
    for sec in rows:
        per_lane: dict = {}
        for e, lane in secs[sec]:
            per_lane.setdefault(lane, []).append(e)
        by_lane[sec] = per_lane
        nboxes = max((len(v) for v in per_lane.values()), default=0)
        # a checkpoint-only second is a thin marker row (no boxes)
        row_h[sec] = (nboxes * BOXH + RGAP) if nboxes else CPH
        row_top[sec] = y
        y += row_h[sec]
    width = AXIS + len(order) * LANEW + 16
    height = y + 12

    def _color(e):
        v = str(e.get("verdict") or "")
        ph = str(e.get("phase") or "")
        if v in ("REJECT", "FAIL", "ERROR"):
            return "var(--err)", "var(--err-bg)"
        if ph in ("research", "drift", "respec", "hitl") \
                or e.get("gate") in ("clarify", "drift", "hitl"):
            return "var(--warn)", "var(--warn-bg)"
        return "var(--ok-border)", "var(--panel)"

    p = [f'<div style="position:relative;width:{width}px;height:{height}px;'
         f'font-size:11px;min-width:{width}px">']
    # left time axis (vertical line + per-row time labels + checkpoint stars) is
    # collected here and emitted in a sticky left:0 band, so it stays put while
    # the lane columns scroll HORIZONTALLY (it still scrolls vertically).
    axis: list = []
    axis.append(f'<div style="position:absolute;left:{AXIS - 1}px;top:{PADT - 6}px;'
                f'width:1px;height:{y - PADT}px;background:var(--border)"></div>')
    # lane labels PINNED to the top: a zero-height sticky band is the absolute
    # positioning context for the names, so they stay visible while the body
    # scrolls vertically (they still scroll horizontally with their columns).
    # Opaque background hides boxes sliding underneath.
    p.append('<div style="position:sticky;top:0;z-index:5;height:0">')
    for lane in order:
        lx = AXIS + lane_x[lane] * LANEW
        p.append(f'<div style="position:absolute;left:{lx}px;top:0;'
                 f'width:{LANEW - 8}px;color:var(--link-2);font-weight:600;overflow:hidden;'
                 f'white-space:nowrap;text-overflow:ellipsis;background:var(--bg);'
                 f'padding:2px 0">{_esc(lane, 90)}</div>')
    p.append('</div>')
    # lane connectors (behind boxes): a line through each lane's box centres
    lane_cy: dict = {}
    for sec in rows:
        for lane, evs in by_lane[sec].items():
            for k in range(len(evs)):
                lane_cy.setdefault(lane, []).append(
                    row_top[sec] + k * BOXH + BOXH / 2)
    for lane, cys in lane_cy.items():
        if len(cys) >= 2:
            lx = AXIS + lane_x[lane] * LANEW + 10
            p.append(f'<div style="position:absolute;left:{lx}px;top:{min(cys):.0f}px;'
                     f'width:2px;height:{max(cys) - min(cys):.0f}px;background:var(--border)"></div>')
    # rows: time label + gridline + stacked boxes (no same-lane overlap)
    for sec in rows:
        top = row_top[sec]
        axis.append(f'<div style="position:absolute;left:0;top:{top}px;'
                    f'width:{AXIS - 6}px;text-align:right;color:var(--dim-2);'
                    f'background:var(--bg)">{hms(sec)}</div>')
        p.append(f'<div style="position:absolute;left:{AXIS}px;top:{top + row_h[sec] - 5}px;'
                 f'width:{width - AXIS}px;height:1px;background:var(--hair)"></div>')
        for lane, evs in by_lane[sec].items():
            for k, e in enumerate(evs):
                yy = top + k * BOXH
                lx = AXIS + lane_x[lane] * LANEW + 6
                border, bg = _color(e)
                v = str(e.get("verdict") or "")
                ph = str(e.get("phase") or "")
                node = _esc(str(e.get("task") or "").split(":")[0] or ph, 90)
                label = (f'<b style="color:var(--fg-strong)">{_esc(ph, 90)} · {node}</b>'
                         f'<br><span style="color:var(--dim)">{_esc(e.get("action") or "", 90)}</span>'
                         + (f' <span style="color:var(--dim-2)">· {_esc(v, 90)}</span>' if v else ''))
                p.append(f'<div style="position:absolute;left:{lx}px;top:{yy}px;'
                         f'width:{LANEW - 18}px;max-height:{BOXH - 6}px;overflow:hidden;'
                         f'background:{bg};border:1px solid {border};border-radius:6px;'
                         f'padding:3px 6px;box-sizing:border-box;line-height:1.2">{label}</div>')
    # checkpoints: a ★ on the time (Y) axis + a dashed horizontal line spanning
    # the whole chart at that instant — NOT a lane/flow of their own.
    for sec in cp_secs:
        if sec not in row_top:
            continue
        cy = row_top[sec] + row_h[sec] / 2
        p.append(f'<div title="чекпойнт {hms(sec)}" style="position:absolute;'
                 f'left:{AXIS}px;top:{cy:.0f}px;width:{width - AXIS}px;height:0;'
                 f'border-top:1px dashed var(--dim);opacity:.55"></div>')
        axis.append(f'<div title="чекпойнт сохранён · {hms(sec)}" '
                    f'style="position:absolute;left:{AXIS - 16}px;top:{cy - 9:.0f}px;'
                    f'color:var(--warn);font-size:13px;line-height:1">★</div>')
    # the left axis pinned horizontally: a sticky left:0 band is the positioning
    # context for the time labels, so they never scroll out sideways.
    p.append('<div style="position:sticky;left:0;z-index:4;width:0;height:0">'
             + "".join(axis) + '</div>')
    p.append('</div>')
    return "".join(p)


# ── orchestra: per-node specialist call sequence (architect→coder→tester→fixer)─
def _orchestra_sequences(llm: list[dict]) -> dict:
    """Reconstruct, per implement node, the ORDERED specialist call sequence of
    an orchestra (D1: architect → coder → tester → fixer, with loops such as
    tester → fixer → tester).

    Why: the flow tab shows node-level milestones; a team node hides WHICH
    specialist ran when and for how long. This exposes the real per-specialist
    sequence so a team run is legible (who, on what model, how long).

    What: returns ``{node_id: [ {step, model, latency_s, wall, t}, ... ]}`` in
    call order. The data comes from ``llm_log.timed_ask``: the per-step ``model``
    + ``step`` live on the ``call_start`` request (mode == "orchestra"); the real
    ``latency_s`` lives on the paired ``call_ok`` (which drops ``step``/``model``
    by design), so we splice the two in arrival order per node. A node listed in
    an ``orchestra_start`` event but with no call records yet appears as an empty
    list (the team is known before any specialist has answered).

    Test: feed an orchestra_start + 4 call_start/call_ok pairs (architect, coder,
    tester, fixer with one tester→fixer→tester loop); assert the returned list is
    ordered, carries each step's model + duration, and the loop repeats `tester`.
    """
    seqs: dict = {}
    # seed known orchestra nodes (team is announced before any call returns)
    for e in llm:
        if e.get("event") == "orchestra_start":
            seqs.setdefault(str(e.get("node")), [])
    # pending call_start per node, paired FIFO with the next call_ok of that node
    pending: dict = {}
    for e in llm:
        ev = e.get("event")
        if ev == "call_start" and e.get("mode") == "orchestra" and e.get("step"):
            node = str(e.get("node"))
            rec = {"step": str(e.get("step")), "model": e.get("model"),
                   "latency_s": None, "wall": e.get("wall"), "t": e.get("t")}
            seqs.setdefault(node, []).append(rec)
            pending.setdefault(node, []).append(rec)
        elif ev == "call_ok" and e.get("mode") == "orchestra":
            node = str(e.get("node"))
            q = pending.get(node)
            if q:
                rec = q.pop(0)
                if isinstance(e.get("latency_s"), (int, float)):
                    rec["latency_s"] = float(e["latency_s"])
                # the call_ok carries the authoritative completion wall clock
                if e.get("wall") is not None:
                    rec["wall"] = e.get("wall")
    return seqs


def _flow_orchestra_html(llm: list[dict]) -> str | None:
    """Per-specialist breakdown for the flow tab — ADDED beneath the existing
    milestone time-axis, never replacing it.

    Why: a solo node already shows one implement milestone; an orchestra node
    must additionally reveal its specialist call sequence with real durations so
    the team's work is visible without redesigning the flow form.

    What: for each implement node that ran an orchestra, renders one card with
    the ordered chain ``architect → coder → tester → fixer`` (loops repeat) on
    the SAME vertical convention as the milestone axis (call order top→bottom),
    each box showing the specialist (its function/role), its model, and the real
    ``latency_s``. Returns None when no orchestra ran (solo run → nothing added).

    Test: a synthetic orchestra llm-log yields a fragment containing each
    specialist name, its model, and its duration; a solo llm-log yields None.
    """
    seqs = _orchestra_sequences(llm)
    seqs = {n: s for n, s in seqs.items() if s}        # drop announced-but-empty
    if not seqs:
        return None


    # function/role glyph per specialist so the chain reads at a glance
    glyph = {"architect": "📐", "coder": "💻", "tester": "🧪", "fixer": "🔧"}
    out = ['<div class=orchestra style="margin-top:14px">',
           '<h4 style="color:var(--link-2);margin:0 0 6px">🎻 Оркестр: '
           'последовательность вызовов специалистов</h4>',
           '<p class=dim style="margin:0 0 8px">для узлов с командой — порядок '
           'специалистов (роль = функция), модель и реальная длительность; '
           'циклы (тестер→ремонтник→тестер) повторяются</p>']
    for node in sorted(seqs):
        steps = seqs[node]
        # a node label must NEVER read «None» — fall back to a step's model or a
        # neutral word if the id is missing (defensive; the engine now always
        # stamps the real node on every orchestra step).
        node_label = node if node and node != "None" else "узел (без id)"
        out.append('<div style="margin:0 0 12px;border:1px solid var(--panel-2);'
                   'border-radius:6px;padding:8px 10px">')
        out.append(f'<div style="color:var(--fg-strong);font-weight:600;margin-bottom:6px">'
                   f'{_esc(node_label)}</div>')
        for i, st in enumerate(steps):
            role = _esc(st.get("step"))
            ico = glyph.get(str(st.get("step")), "•")
            model = _esc(st.get("model") or "—")
            lat = st.get("latency_s")
            dur = f'{lat:.1f}s' if isinstance(lat, (int, float)) else '—'
            arrow = ('<div style="color:var(--border);margin:1px 0 1px 6px">↓</div>'
                     if i else '')
            out.append(arrow)
            out.append(
                f'<div style="background:var(--panel);border:1px solid var(--ok-border);'
                f'border-radius:6px;padding:4px 8px;line-height:1.3">'
                f'<b style="color:var(--fg-strong)">{ico} {role}</b>'
                f' <span style="color:var(--dim)">· {model}</span>'
                f' <span style="color:var(--dim-2)">· {dur}</span></div>')
        out.append('</div>')
    out.append('</div>')
    return "".join(out)


def _flow_html(events: list[dict], tree: dict | None,
               llm: list[dict]) -> str | None:
    """Compose the flow tab: the EXISTING milestone time-axis (unchanged form)
    plus, when a team ran, the per-specialist orchestra breakdown beneath it.

    Why: keep the established flow representation and ADD the specialist sequence
    inside the same tab (the user asked to extend, not redesign the flow tab).

    What: returns the time-axis fragment, the orchestra fragment, both, or None.

    Test: a solo run (no orchestra) returns the time-axis only; an orchestra run
    appends the specialist-sequence fragment after it.
    """
    axis = _flow_timeaxis(events, tree) if events else None
    orch = _flow_orchestra_html(llm) if llm else None
    if axis and orch:
        return axis + orch
    return axis or orch


# ── state ────────────────────────────────────────────────────────────────────
def _pid_alive(run_dir: "pathlib.Path | None") -> bool:
    """True when the run's process (run.pid) is alive. Used only for the
    stop-signal mechanics, never for the liveness badge — see _run_active.

    NOTE: run.pid stores os.getpid() as seen INSIDE the run's PID namespace
    (a small number like 22 under a sandbox), so os.kill() from a dashboard in
    another namespace tests an unrelated host PID and reports a dead run as
    alive. Liveness for display/selection must therefore not rely on this."""
    if run_dir is None:
        return False
    pidf = run_dir / "run.pid"
    if not pidf.exists():
        return False
    try:
        os.kill(int(pidf.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


# A run is live while it has not written its final SUMMARY.md and is still
# emitting trace lines. The window must exceed the worst silent gap — a full
# provider+model fallback chain on the slow free pool (~3 x 80s) — so an
# actively-stalling run is not declared dead mid-call.
_RUN_FRESH_S = 300.0


def _run_active(run_dir: "pathlib.Path | None") -> bool:
    """Namespace-proof liveness: no SUMMARY.md yet AND trace.jsonl was touched
    within _RUN_FRESH_S. Unlike _pid_alive this never cross-checks a PID, so a
    sandboxed run's namespace-local run.pid cannot masquerade as a live host
    process (the false-'alive' a finished v022 showed)."""
    if run_dir is None:
        return False
    if (run_dir / "SUMMARY.md").exists():
        return False
    try:
        age = _time.time() - (run_dir / "trace.jsonl").stat().st_mtime
    except OSError:
        return False
    return age < _RUN_FRESH_S


def _build_state(run_dir: pathlib.Path) -> dict:
    events = _read_jsonl(run_dir / "trace.jsonl")
    llm = _read_jsonl(run_dir / "llm-log.jsonl")
    ws = run_dir / "workspace"
    done = (run_dir / "SUMMARY.md").exists()

    tree = (_tree_from_file(run_dir) or _tree_from_decomp(run_dir)
            or _tree_from_llm(llm))
    # surface late-injected requirement nodes (web_ui, seller_directory) that
    # the decompose tree never knew — otherwise they run but stay invisible
    _attach_orphan_nodes(tree, events)
    # also surface requirements that were INJECTED (a folder under
    # hitl/requirements/) but not yet materialized into a node — the engine
    # only places them at the next branch/root integrate, so without this the
    # user injects web_ui and sees nothing in the graph until much later
    _attach_pending_requirements(tree, run_dir)
    meta: dict = {}
    _flatten(tree, 0, meta, None)
    files = {nid: _node_files(ws, nid) for nid in meta} if ws.exists() else {}
    ev_idx = _events_by_node(events, llm)

    # error badges: any REJECT/FAIL/ERROR verdict on the node's events marks
    # it on the tree + spec graph; PRUNED (dedup gate) gets its own badge
    bad = {"REJECT", "FAIL", "ERROR"}
    for nid, evs in ev_idx.items():
        if nid not in meta:
            continue
        eps = meta[nid]["episodes"]
        # PER GATE only the LATEST verdict counts — spec_review rework,
        # spec_lint fix rounds and integrate repairs all clear the red
        # badge once the same gate passes (the node page already grouped
        # them as fixed history; the tree showed ❌ regardless — they
        # must agree)
        latest_by_gate = {}
        for e in evs:
            if e.get("gate"):
                latest_by_gate[e["gate"]] = str(e.get("verdict") or "")
        gate_bad = any(v in bad for v in latest_by_gate.values())
        loose_bad = any(str(e.get("verdict")) in bad
                        for e in evs if not e.get("gate"))
        had_bad = any(str(e.get("verdict")) in bad for e in evs)
        if (gate_bad or loose_bad) and "error" not in eps:
            eps.append("error")
        elif had_bad and "error" not in eps and "reworked" not in eps:
            # bad history, green now — the visible 'fixed' mark
            eps.append("reworked")
        if any(str(e.get("verdict")) == "PRUNED" for e in evs) and "pruned" not in eps:
            eps.append("pruned")

    # research spikes live in events as '<node>:spike' tasks — surface them
    # as a 🔬 badge on the owning node (the live tree has no spike field;
    # NB ev_idx groups by the BASE node id, so scan raw events here)
    for e in events:
        task = str(e.get("task") or "")
        if task.endswith(":spike"):
            owner = task[:-6]
            if owner in meta and "spike" not in meta[owner]["episodes"]:
                meta[owner]["episodes"].insert(0, "spike")

    feed = []
    for e in llm:
        if e.get("event") != "outcome":
            continue
        if e.get("role") == "decomposer":
            v = e.get("verdict")
            kids = ", ".join(e.get("children") or [])
            feed.append(f"{'🍃' if v == 'leaf' else '🌿'} {e.get('node')} d{e.get('depth')}"
                        f" → {v}" + (f" → {kids}" if kids else ""))
        elif e.get("role") == "implementer":
            ok = e.get("parse") == "ok"
            feed.append(f"{'💻' if ok else '⚠️'} {e.get('node')} "
                        + (f"код {e.get('code_chars','?')}c · тест {e.get('test_chars','?')}c"
                           if ok else f"отклонено: {str(e.get('error',''))[:80]}"))

    try:
        report_md = T.build_run_report(events, level=2, title=run_dir.name) \
            if events else ""
        if report_md and not done:
            # the audit checks COMPLETED-run invariants; on a live run the
            # completion rules (integrate per branch, L0 complete) have not
            # happened YET — show those rows as pending, not as violations
            report_md = "\n".join(
                ln.replace("❌ error", "⏳ ждёт финала", 1)
                if "R5-branch-no-integrate" in ln else ln
                for ln in report_md.splitlines())
        report_html = _md_to_html(report_md) if report_md \
            else "<p class=dim>событий ещё нет…</p>"
        if events and not done:
            report_html = (
                "<p class=muted>⏳ прогон ещё идёт: интеграция у веток — "
                "снизу вверх, поэтому R5 у незакрытых веток помечен «ждёт "
                "финала». Судить аудит — после завершения.</p>" + report_html)
    except Exception as exc:                                       # noqa: BLE001
        report_html = f"<p>report error: {html.escape(str(exc))}</p>"

    def read_md(name):
        f = run_dir / name
        return _md_to_html(f.read_text(encoding="utf-8")) if f.exists() else None

    # current activity + chronological timeline (orient: done / happening now)
    current = "✅ завершён" if done else "…"
    # ALL open calls — under parallel children several are in flight at
    # once; a single last-event tracker showed just one and any sibling's
    # outcome wiped it
    open_calls = {}
    for e in llm:
        key = (e.get("node"), e.get("role"))
        if e.get("event") == "call_start":
            open_calls[key] = e
        elif e.get("event") in ("call_ok", "call_error", "outcome"):
            # a call closes with call_ok/call_error (per LLM call); 'outcome' is
            # a per-node summary that does not fire for every call. Popping only
            # on 'outcome' left ~half the calls dangling 'open' forever — incl.
            # the synthetic 'late-requirement' (amend-route) call, which then
            # showed as the active node for the whole run. Pop on any completion.
            open_calls.pop(key, None)
    # The orchestra emits SEVERAL call_start per (node, role) — architect / coder
    # / tester / fixer and retries — but only ONE 'outcome', so a done node can
    # keep a dangling open call and look like it is still executing. Reconcile
    # against the node's LATEST lifecycle state in the trace: a node that already
    # reached a terminal state is NOT active, whatever the llm-log left open.
    _TERMINAL = {"to_done", "to_failed", "to_halted", "done", "to_integrated"}
    node_state = {}
    for e in events:
        if e.get("phase") == "lifecycle":
            nn = str(e.get("task") or e.get("node") or "").split(":")[0]
            if nn:
                node_state[nn] = str(e.get("action") or e.get("state") or "")
    open_calls = {
        k: e for k, e in open_calls.items()
        if node_state.get(str(e.get("node") or "").split(":")[0]) not in _TERMINAL}
    last_start = list(open_calls.values())[-1] if open_calls else None
    _ROLE_RU = {"decomposer": "декомпозирует", "implementer": "пишет код",
                "reviewer": "ревьюит", "researcher": "исследует"}
    if not done:
        if open_calls:
            parts = []
            for e in open_calls.values():
                # never show «None» — fall back to the call's title, then its
                # step (specialist), then a dash; a bare «—» means an LLM call
                # reached the dashboard with no node attribution (a bug upstream)
                _n = e.get("node")
                node = (str(_n) if _n not in (None, "None", "")
                        else (e.get("title") or e.get("step") or "—"))
                lvl = e.get("depth")
                if not (isinstance(lvl, int) and lvl >= 0):
                    # older harness logs carry no depth — the TREE knows it
                    lvl = (meta.get(node) or {}).get("depth")
                chip = (f" (L-{lvl})"
                        if isinstance(lvl, int) and lvl >= 0 else "")
                parts.append(f"{_ROLE_RU.get(e.get('role'), e.get('role'))}"
                             f" «{node}»{chip}")
            current = "🟢 " + "  ⏐  ".join(parts)
        elif events:
            le = events[-1]
            current = f"🟢 {le.get('phase')}: {le.get('action')}"
    timeline = [{"tick": e.get("tick"), "t": e.get("t"), "phase": e.get("phase"),
                 "node": e.get("task"),
                 "text": str(e.get("action")), "verdict": e.get("verdict")}
                for e in events if int(e.get("level") or 2) <= 2]

    inputs_goal = ""
    if (run_dir / "inputs.json").exists():
        try:
            inputs_goal = json.loads(
                (run_dir / "inputs.json").read_text(encoding="utf-8")).get("goal", "")
        except json.JSONDecodeError:
            pass

    active = None
    actives = []
    if not done and open_calls:
        # REAL elapsed per open call: llm-log t is run-relative, the
        # first trace event carries the run-start epoch — a page reload
        # shows the true age, not 'since the client noticed'
        t0 = events[0].get("t") if events else None
        for e in open_calls.values():
            a = {"node": e.get("node"), "role": e.get("role")}
            rel = e.get("t")
            if t0 is not None and rel is not None:
                a["elapsed_s"] = max(
                    0.0, _time.time() - (float(t0) + float(rel)))
            actives.append(a)
        active = actives[-1]
    elif not done and events:
        # fallback for runs whose workers do not bracket sessions with
        # call_start (older harness in a live process): the tail of the
        # trace is the best approximation of where the pipeline is
        last_task = str(events[-1].get("task") or "").split(":")[0]
        if last_task:
            active = {"node": last_task, "role": events[-1].get("phase")}
            t_last = events[-1].get("t")
            if t_last is not None:
                active["elapsed_s"] = max(0.0,
                                          _time.time() - float(t_last))
            actives = [active]
    return {
        "name": run_dir.name,
        "status": "done" if done else "running",
        # actual process liveness (run.pid), not just trace state — gates the
        # dashboard's stop/run buttons. A killed/crashed run is NOT active even
        # if its last trace line never said 'done'.
        "run_active": _run_active(run_dir),
        "goal": inputs_goal,
        "current": current,
        "active": active,
        "actives": actives,
        "counts": {
            "nodes": sum(1 for e in llm if e.get("event") == "outcome"
                         and e.get("role") == "decomposer") or len(meta),
            "leaves": sum(1 for m in meta.values() if m["verdict"] == "leaf"),
            "impl": sum(1 for e in llm if e.get("event") == "outcome"
                        and e.get("role") == "implementer"),
            "events": len(events),
        },
        "tree": _tree_view(tree, meta),
        "nodes": {nid: {**meta[nid], "files": files.get(nid, {}),
                        "events": ev_idx.get(nid, [])} for nid in meta},
        "feed": feed[-60:],
        "timeline": timeline[-250:],
        "reports": {
            "inputs": _md_to_html(_inputs_md(run_dir, llm)),
            "flow": _flow_html(events, tree, llm),
            "report": report_html,
            "oracle": read_md("oracle-report.md"),
            "summary": read_md("SUMMARY.md"),
            "commits": read_md("workspace/COMMITS.md"),
            "workflow": read_md("workflow.md"),
        },
    }


# llm-log events that RECORD A COMPLETION (not a new request). Kept as a set so
# a start/result pair counts as one request; extend if a worker adds another
# terminal name. Everything else carrying a `model` field is a request start.
_LLM_RESULT_EVENTS = {"outcome", "llm_attempt", "llm_fallback"}

# fields that are NOT categorical dimensions: the timestamp and free-text /
# high-cardinality payload. Everything else a call logs IS a dimension — the
# list of dimensions is discovered from the data, never hard-coded, so a new
# logged parameter (solo/orchestra, retry number, ...) becomes a slice on its
# own with no change here.
_LLM_NON_DIMS = {"t", "reason", "detail", "text", "error", "files", "idx",
                 "_ev", "latency_s", "wall", "wall_end", "reply_chars",
                 "prompt_chars"}


def _llm_usage(calls: list, dims=None) -> dict:
    """Multi-parameter LLM-usage rollup over an OPEN dimension set.

    Returns {total, dims: [...], by_<dim>: [{key, count}]}. When `dims` is None
    the dimensions are DISCOVERED as the union of categorical keys present
    across the calls (minus _LLM_NON_DIMS) — so usage can be read by role, by
    specialty, by model, by node, or by any future parameter, with no edit
    here. Pass `dims` to restrict/order them."""
    if dims is None:
        seen: set = set()
        for c in calls:
            seen |= set(c.keys())
        dims = sorted(d for d in seen if d not in _LLM_NON_DIMS)
    out: dict = {"total": len(calls), "dims": list(dims)}
    for d in dims:
        agg: dict = {}
        for c in calls:
            v = c.get(d)
            if v is None or v == "":
                continue
            agg[str(v)] = agg.get(str(v), 0) + 1
        out["by_" + d] = sorted(
            ({"key": k, "count": n} for k, n in agg.items()),
            key=lambda x: x["count"], reverse=True)
    return out


def _idle_analysis(run_dir: "pathlib.Path | None", top: int = 40) -> dict:
    """Duration / idle analysis for the ⏱ tab. Each trace event's gap to the
    NEXT event is the time that operation took; we attribute it to the owner
    event (its node + phase + action) and classify the CAUSE of the spend:

      * 'ожидание квоты'   — a quota_wait/error_round overlapped the gap;
      * 'интеграция'       — integrate gate (pytest over the built tree);
      * 'починка'          — a rework/repair round;
      * 'ревью спеки'      — review phase;
      * 'реализация (LLM)' — implement phase;
      * 'декомпозиция'     — decompose;
      * 'ответ человека'   — a HITL ask was open;
      * otherwise the phase name.

    Returns sortable rows + roll-ups by node, by phase, by cause. All purely
    from artifacts, no run interaction."""
    if run_dir is None:
        return {"empty": True}
    trace = []
    tf = run_dir / "trace.jsonl"
    if tf.exists():
        for line in tf.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trace.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # quota / error wait windows + LLM call timestamps from the llm-log.
    # The trace stamps events in EPOCH seconds, but the llm-log stamps them
    # RUN-RELATIVE (0.003, 253.5, ...). Comparing the two bases directly made
    # every "calls inside this gap" test false → the LLM-request column read 0
    # for decompose/review/implement even though those ARE LLM calls. Normalise
    # the llm-log timestamps onto the epoch axis via the run's start epoch.
    run_start = float(trace[0].get("t") or 0.0) if trace else 0.0

    def _epoch(t: float) -> float:
        # already epoch (older harness) vs run-relative (current): a value far
        # below the run start is run-relative seconds → shift onto the axis
        return t if t >= run_start else run_start + t

    waits = []
    calls = []
    # FAILURE report: when a model did NOT answer normally (429/5xx/timeout/empty)
    # — how many times, what it led to (a fallback to another model, or a terminal
    # error) and the delay it cost. Fed by the `llm_attempt` (abnormal) and
    # `llm_fallback` records the backend now emits.
    fail: dict = {}
    transitions: list = []

    def _fm(model: str) -> dict:
        return fail.setdefault(model or "—", {
            "model": model or "—", "abn": 0, "429": 0, "5xx": 0,
            "timeout": 0, "empty": 0, "fallbacks": 0, "errors": 0,
            "delay_s": 0.0})

    # call_error carries no `model` (the request marker lives on the preceding
    # call_start), so remember the last model seen per identity and attribute
    # the failure to it. This is how the report populates on the LIVE path,
    # which emits call_error / provider_fallback — NOT the llm_attempt /
    # llm_fallback records (those only fire when a call goes through the
    # llm_backend.ask retry chain, which the orchestra/provider path bypasses).
    last_model: dict = {}

    def _err_class(s: str) -> str:
        s = (s or "").lower()
        if "429" in s or "rate limit" in s or "too many" in s or "quota" in s:
            return "429"
        if any(c in s for c in ("500", "502", "503", "504", "bad gateway",
                                "server error")):
            return "5xx"
        if any(c in s for c in ("timeout", "timed out", "refused",
                                "connection", "unreachable", "temporarily")):
            return "timeout"
        if any(c in s for c in ("empty", "malformed", "no verdict",
                                "reasoning", "could not parse")):
            return "empty"
        return ""

    # completion records (call_ok / call_error) carry the real `latency_s` (how
    # long the model actually worked) but NOT `model`, so they are not request
    # records themselves. We queue them per identity and later splice their
    # latency onto the matching call_start, giving each request a true duration.
    _COMPLETION_EVENTS = {"call_ok", "call_error"}

    def _ident_key(e: dict) -> tuple:
        return tuple(e.get(k) for k in ("role", "node", "depth", "purpose", "mode"))

    completions: dict = {}
    lf = run_dir / "llm-log.jsonl"
    if lf.exists():
        for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_ = e.get("event")
            if ev_ in ("quota_wait", "error_round"):
                t = e.get("t")
                if t is not None:
                    waits.append((_epoch(float(t)),
                                  float(e.get("wait_s", 0) or 0)))
            elif ev_ == "llm_attempt" and e.get("abnormal"):
                d = _fm(e.get("model"))
                d["abn"] += 1
                d["delay_s"] += float(e.get("slept_s", 0) or 0)
                st = e.get("status")
                if st == 429:
                    d["429"] += 1
                elif isinstance(st, int) and 500 <= st <= 599:
                    d["5xx"] += 1
                elif st == 0:
                    d["timeout"] += 1
                if e.get("error") == "empty":
                    d["empty"] += 1
            elif ev_ == "llm_fallback":
                frm = e.get("from_model")
                transitions.append({"from": frm, "to": e.get("to_model"),
                                    "reason": e.get("reason"),
                                    "terminal": bool(e.get("terminal"))})
                if frm:
                    d = _fm(frm)
                    d["fallbacks"] += 1
                    if e.get("terminal"):
                        d["errors"] += 1
            elif ev_ in _COMPLETION_EVENTS:
                lat = e.get("latency_s")
                if lat is not None:
                    completions.setdefault(_ident_key(e), []).append(
                        {"latency_s": float(lat),
                         "wall": e.get("wall")})
                # a call_error IS an abnormal response on the live path; the
                # model is on the preceding call_start (see last_model).
                if ev_ == "call_error":
                    mdl = last_model.get(_ident_key(e)) or "—"
                    d = _fm(mdl)
                    d["abn"] += 1
                    d["errors"] += 1
                    d["delay_s"] += float(lat or 0)
                    cls = _err_class(e.get("error") or "")
                    if cls:
                        d[cls] += 1
            elif ev_ == "provider_fallback":
                # a remote provider was unreachable and the run degraded to the
                # local model — an abnormal outcome with a real transition.
                mdl = e.get("model") or "—"
                d = _fm(mdl)
                d["abn"] += 1
                d["fallbacks"] += 1
                cls = _err_class(e.get("error") or "")
                if cls:
                    d[cls] += 1
                transitions.append({"from": e.get("provider") or mdl,
                                    "to": mdl, "reason": cls or "provider down",
                                    "terminal": False})
            elif "model" in e and ev_ not in _LLM_RESULT_EVENTS:
                last_model[_ident_key(e)] = e.get("model")
                # UNIVERSAL, role-independent LLM-request signal: every model
                # invocation names its `model`, whatever the worker calls the
                # event (call_start for decomposer/reviewer, orchestra_step for
                # an implementer team, creator_candidate for the ensemble, and
                # any FUTURE orchestra step — all carry `model`). Bookkeeping
                # events (claim, commit_queue, ws_write, *_start markers) have
                # no model and are ignored. Terminal result records are excluded
                # so a start/result pair counts as ONE request. Counting only
                # call_start used to make the implement phase read 0 LLM calls.
                #
                # Capture the WHOLE event as the call record (only `event` is
                # dropped, `t` is normalised onto the epoch axis). The set of
                # dimensions is therefore OPEN — role, model, node, specialty
                # today; solo/orchestra, retry number, anything a caller logs
                # tomorrow — all become slice-able automatically, no schema to
                # edit here. We do not know the final list, so we do not pin it.
                t = e.get("t")
                if t is not None:
                    rec = dict(e)
                    # keep the event kind under a reserved key so the call-cause
                    # classifier can tell an orchestra_step / creator_candidate
                    # (implement stage) apart from a plain call_start, without
                    # re-reading the log. `event` itself is dropped so it does
                    # not become a usage dimension.
                    rec["_ev"] = rec.pop("event", None)
                    rec["t"] = _epoch(float(t))
                    calls.append(rec)

    # splice each completion's real latency onto its matching call_start request
    # (same identity, FIFO order). timed_ask logs latency on call_ok, which has
    # no `model` and is therefore not a request itself — without this join the
    # request records would have no duration and the LLM-time column would stay
    # gap-sourced even on a harness that DOES record real call durations.
    _comp_idx: dict = {}
    for c in calls:
        if c.get("latency_s") is not None:
            continue
        k = tuple(c.get(kk) for kk in ("role", "node", "depth", "purpose", "mode"))
        q = completions.get(k)
        if not q:
            continue
        i = _comp_idx.get(k, 0)
        if i < len(q):
            c["latency_s"] = q[i]["latency_s"]
            if q[i].get("wall") is not None:
                c["wall_end"] = q[i]["wall"]
            _comp_idx[k] = i + 1

    def _cause(ev: dict, gap: float, t0: float, t1: float) -> str:
        for wt, _ws in waits:
            if t0 <= wt <= t1:
                return "ожидание квоты"
        ph = str(ev.get("phase", ""))
        act = str(ev.get("action", "")).lower()
        gate = str(ev.get("gate", ""))
        if "rework" in act or "repair" in act:
            return "починка"
        if ph == "integrate" or "integrate" in act or "acceptance" in act:
            return "интеграция (тесты)"
        if ph == "review":
            return "ревью спеки"
        if ph == "implement":
            return "реализация (LLM)"
        if ph in ("decompose", "expand"):
            return "декомпозиция (LLM)"
        if ph == "hitl" or gate == "hitl":
            return "ответ человека"
        return ph or "прочее"

    # ---- authoritative LLM attribution, keyed by node+role (not trace gaps) --
    # The trace milestones for the implement stage are written in a BURST after
    # the orchestra returns, so they share ~one timestamp; the gap between them
    # is <=0 and the row is dropped below. Counting "calls inside this gap" then
    # loses every orchestra request. So we attribute LLM requests (and, when the
    # harness logged real durations, LLM time) to a cause DIRECTLY from the
    # llm-log call records via their role/event — independent of trace gaps.
    _IMPL_ROLES = {"implementer", "architect", "coder", "tester", "fixer"}
    _IMPL_EVENTS = {"orchestra_step", "orchestra_start",
                    "creator_candidate", "creator_ensemble"}

    def _call_cause(rec: dict):
        """Map a single llm-log call record to its idle cause via role/event.
        Returns None when the call is ambiguous (no role, no orchestra marker)
        — those keep the timestamp-window attribution instead, so a role-less
        call is never force-folded into реализация."""
        role = str(rec.get("role", "")).lower()
        kind = str(rec.get("_ev", "")).lower()
        if role == "decomposer":
            return "декомпозиция (LLM)"
        if role == "reviewer":
            return "ревью спеки"
        if role == "verifier":
            return "интеграция (тесты)"
        if (role in _IMPL_ROLES or kind in _IMPL_EVENTS
                or rec.get("orchestra")):
            return "реализация (LLM)"
        return None

    # cause -> {req, time, nodes}: real request count, real model time (summed
    # call durations when the harness logged them, else 0 = unknown), and the
    # set of distinct nodes that issued calls (one logical op per node).
    llm_by_cause: dict = {}
    for c in calls:
        cc = _call_cause(c)
        if cc is None:
            continue
        a = llm_by_cause.setdefault(
            cc, {"req": 0, "time": 0.0, "nodes": set(), "timed": False})
        a["req"] += 1
        # prefer an explicit latency; fall back to wall_end-wall_start if both
        # endpoints were logged. Old runs have neither → time stays 0 (unknown)
        # and the trace-gap time is kept for that cause.
        lat = c.get("latency_s")
        if lat is None and c.get("wall") is not None and c.get("wall_end") is not None:
            lat = float(c["wall_end"]) - float(c["wall"])
        if lat is not None:
            a["time"] += float(lat)
            a["timed"] = True
        nd = c.get("node")
        if nd:
            a["nodes"].add(nd)

    # calls we could NOT attribute by role/event (no role, no orchestra marker).
    # Only THESE keep the old timestamp-window attribution; calls with a known
    # cause are counted authoritatively above and must not also leak into a gap
    # they happen to fall inside (that was the orchestra-into-wrong-cause bug).
    _ambig_t = [c["t"] for c in calls if _call_cause(c) is None]

    rows = []
    for i in range(len(trace) - 1):
        ev, nxt = trace[i], trace[i + 1]
        t0, t1 = ev.get("t"), nxt.get("t")
        if t0 is None or t1 is None:
            continue
        gap = float(t1) - float(t0)
        if gap <= 0:
            continue
        # how many UN-attributable LLM calls were started inside this gap — lets
        # the idle table still show request activity for causes the llm-log
        # cannot key by role, without double-counting role-keyed calls.
        llm = sum(1 for t in _ambig_t if float(t0) <= t < float(t1))
        # A gap is the time the engine spent PRODUCING the next event, so it is
        # attributed to that NEXT event's work — not the previous one. (Otherwise
        # the idle after an auto-approved HITL checkpoint is mislabelled "ответ
        # человека" when the engine was actually decomposing/implementing.)
        rows.append({
            "tick": nxt.get("tick"),
            "node": nxt.get("task", ""),
            "phase": nxt.get("phase", ""),
            "action": str(nxt.get("action", ""))[:60],
            "dur": round(gap, 1),
            "cause": _cause(nxt, gap, float(t0), float(t1)),
            "llm": llm,
        })

    def _rollup(key: str) -> list:
        agg: dict = {}
        for r in rows:
            k = r[key] or "—"
            a = agg.setdefault(k, {"total": 0.0, "count": 0, "llm": 0})
            a["total"] += r["dur"]
            a["count"] += 1
            a["llm"] += int(r.get("llm", 0) or 0)
        out = [{key: k, "total": round(v["total"], 1),
                "count": v["count"], "llm": v["llm"]}
               for k, v in agg.items()]
        return sorted(out, key=lambda x: x["total"], reverse=True)

    def _cause_rollup() -> list:
        """by_cause with AUTHORITATIVE LLM attribution from the llm-log.

        Time comes from trace gaps (the wall-clock the engine spent), but for
        LLM-bearing causes the request count, op count and — when the harness
        logged real call durations — the time are taken from the llm-log call
        records keyed by node/role, NOT from how many trace gaps happened to
        survive the burst-collapse. This is what fixes 'реализация (LLM): 0
        запросов LLM' even though the orchestra made ~23 real calls."""
        # интеграция = mostly pytest over the built tree; its wall time must
        # stay trace-gap-sourced even though the verifier also makes one LLM
        # judgment call per node. For it we override only the request count, not
        # the time/op-count. The pure-LLM causes below own their whole window.
        _PURE_LLM = {"декомпозиция (LLM)", "ревью спеки", "реализация (LLM)"}
        base = {r["cause"]: r for r in _rollup("cause")}
        # fold every cause seen in the llm-log in, even if its trace rows were
        # all dropped (e.g. orchestra milestones that collapsed to one tick).
        for cc, lc in llm_by_cause.items():
            r = base.get(cc)
            if r is None:
                r = {"cause": cc, "total": 0.0, "count": 0, "llm": 0}
                base[cc] = r
            # request count: real number of model calls for this cause —
            # always authoritative from the llm-log.
            r["llm"] = lc["req"]
            if cc in _PURE_LLM:
                # op count: one logical op per node that issued calls (an
                # implement node is ONE op, not an artifact of surviving gaps).
                if lc["nodes"]:
                    r["count"] = len(lc["nodes"])
                # time: prefer summed real call durations when the harness
                # logged them; otherwise keep the trace-gap wall time.
                if lc["timed"] and lc["time"] > 0:
                    r["total"] = round(lc["time"], 1)
        out = list(base.values())
        return sorted(out, key=lambda x: x["total"], reverse=True)

    total = round(sum(r["dur"] for r in rows), 1)
    top_rows = sorted(rows, key=lambda r: r["dur"], reverse=True)[:top]
    return {
        "total_s": total,
        "events": len(rows),
        "top": top_rows,
        "by_node": _rollup("node")[:25],
        "by_phase": _rollup("phase"),
        "by_cause": _cause_rollup(),
        "tokens": _token_economics(run_dir),
        # headline usage-per-LLM table (provider+model): requests, tokens
        # in/out, wall time — the "who did the work" table for the Простои tab.
        "agent_usage": _llm_agent_usage(run_dir),
        # multi-parameter LLM-usage breakdown (by role / specialty / model /
        # node) — the data side of the multi-axis analysis; the UI can slice it
        # any way without re-reading the log.
        "llm_usage": _llm_usage(calls),
        # FAILURE report — abnormal responses → fallback / error, with delay
        "failures": {
            "by_model": sorted(fail.values(),
                               key=lambda r: r["delay_s"], reverse=True),
            "transitions": transitions[-60:],
            "totals": {
                "abnormal": sum(r["abn"] for r in fail.values()),
                "fallbacks": sum(r["fallbacks"] for r in fail.values()),
                "terminal": sum(1 for tr in transitions if tr["terminal"]),
                "delay_s": round(sum(r["delay_s"] for r in fail.values()), 1),
            },
        },
    }


def _token_economics(run_dir: "pathlib.Path | None") -> dict:
    """#6: real token spend rolled up by role+model from token_usage events
    (the API usage field). Returns {rows: [{role, model, calls, prompt,
    completion, total}], total}. Empty when the provider reported no usage."""
    if run_dir is None:
        return {"rows": [], "total": 0}
    lf = run_dir / "llm-log.jsonl"
    if not lf.exists():
        return {"rows": [], "total": 0}
    agg: dict = {}
    grand = 0
    for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") != "token_usage":
            continue
        # An ORCHESTRA specialist step (architect/coder/tester/fixer) is a
        # SPECIALIST inside a stage -> stage = the role's stage (реализация),
        # specialist column = the step. An ENGINE step (e.g. amend-route) is its
        # OWN stage performed BY a role -> stage = the step, specialist = the role.
        step = e.get("step")
        role = e.get("role")
        if step and step not in _ORCHESTRA_STEPS:
            stage = step                       # the engine step IS the stage
            who = role or "—"                  # the role is the worker (specialist)
        else:
            stage = _STAGE_RU.get(role, role or "—")
            who = step or role or "—"          # the orchestra specialist (or role)
        key = (stage, who, e.get("model") or "—")
        a = agg.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0,
                                 "estimated": False})
        pt = int(e.get("prompt_tokens", 0) or 0)
        ct = int(e.get("completion_tokens", 0) or 0)
        a["calls"] += 1
        a["prompt"] += pt
        a["completion"] += ct
        if e.get("estimated"):
            a["estimated"] = True
        grand += pt + ct
    rows = [{"stage": stg, "role": who, "model": m, "calls": v["calls"],
             "prompt": v["prompt"], "completion": v["completion"],
             "total": v["prompt"] + v["completion"],
             "estimated": v["estimated"]}
            for (stg, who, m), v in agg.items()]
    rows.sort(key=lambda x: x["total"], reverse=True)
    return {"rows": rows, "total": grand}


def _llm_agent_usage(run_dir: "pathlib.Path | None") -> dict:
    """Headline 'who did the work' table — usage per LLM (provider+model):
    request count and wall time come from llm_attempt (it carries model +
    latency_s), prompt/completion tokens come from token_usage (model + usage).
    Joined on the model id. Returns {rows:[{provider, model, requests, prompt,
    completion, total, time_s, abnormal, estimated}], totals:{...}}."""
    if run_dir is None:
        return {"rows": [], "totals": {}}
    lf = run_dir / "llm-log.jsonl"
    if not lf.exists():
        return {"rows": [], "totals": {}}
    agg: dict = {}

    def _row(model: str) -> dict:
        return agg.setdefault(model, {
            "provider": "", "requests": 0, "prompt": 0, "completion": 0,
            "time_s": 0.0, "abnormal": 0, "estimated": False})

    for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = e.get("event")
        if ev == "llm_attempt":
            # canonicalise a bare model id ('haiku') to 'provider/model' so it
            # joins token_usage's prefixed id ('claude/haiku') — older logs wrote
            # the short id here (the source now logs the canonical one).
            _m = e.get("model") or "—"
            _p = e.get("provider")
            if _p and "/" not in _m:
                _m = f"{_p}/{_m}"
            r = _row(_m)
            r["requests"] += 1
            r["time_s"] += float(e.get("latency_s", 0) or 0)
            if not r["provider"] and e.get("provider"):
                r["provider"] = e.get("provider")
            if e.get("abnormal"):
                r["abnormal"] += 1
        elif ev == "token_usage":
            r = _row(e.get("model") or "—")
            r["prompt"] += int(e.get("prompt_tokens", 0) or 0)
            r["completion"] += int(e.get("completion_tokens", 0) or 0)
            if e.get("estimated"):
                r["estimated"] = True
    rows = [{"model": m, "provider": v["provider"] or "—",
             "requests": v["requests"], "prompt": v["prompt"],
             "completion": v["completion"],
             "total": v["prompt"] + v["completion"],
             "time_s": round(v["time_s"], 1), "abnormal": v["abnormal"],
             "estimated": v["estimated"]}
            for m, v in agg.items()]
    rows.sort(key=lambda x: x["time_s"], reverse=True)
    totals = {
        "requests": sum(r["requests"] for r in rows),
        "tokens": sum(r["total"] for r in rows),
        "time_s": round(sum(r["time_s"] for r in rows), 1),
        "abnormal": sum(r["abnormal"] for r in rows),
    }
    return {"rows": rows, "totals": totals}


def _hitl_state(run_dir: "pathlib.Path | None") -> dict:
    """Snapshot the bidirectional HITL channel for the ✋ tab:
      * asks      — worker→human questions parsed from hitl/questions.md
                    (each '[HITL?]'/'asks:' block, newest last);
      * answered  — log of '**answer...**' lines already recorded;
      * pending   — answer.md is present and non-empty (a reply written but
                    not yet consumed by the worker);
      * requirements — late requirements already injected (folder names).
    Read-only; the human acts through the POST endpoints."""
    if run_dir is None:
        return {"empty": True}
    hitl = run_dir / "hitl"
    asks, answered = [], []
    qmd = hitl / "questions.md"
    qmtime = qmd.stat().st_mtime if qmd.exists() else 0.0

    def _ask_epoch(hdr):
        # the header carries a [HH:MM:SS] stamp; pin it to the file's date
        if qmtime and "[" in hdr and "]" in hdr:
            tod = hdr[hdr.find("[") + 1:hdr.find("]")].split(":")
            if len(tod) == 3 and all(p.isdigit() for p in tod):
                lt = _time.localtime(qmtime)
                try:
                    return _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                         int(tod[0]), int(tod[1]), int(tod[2]),
                                         0, 0, -1))
                except (ValueError, OverflowError):
                    pass
        return qmtime

    if qmd.exists():
        # Block format: a '## [time] role @ node' header, then the question
        # body lines, then optional '**answer ...**' lines. Parse into
        # structured asks {header, t, body, answer, answered} so the dashboard
        # shows the actual question text, not just the header.
        cur = None
        for line in qmd.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.rstrip()
            st = s.strip()
            if st.startswith("##") or "asks:" in st or "[HITL?]" in st:
                if cur:
                    asks.append(cur)
                hdr = st.lstrip("# ").strip()
                cur = {"header": hdr, "t": _ask_epoch(hdr),
                       "body": "", "answer": "", "answered": False}
            elif st.startswith("**answer"):
                answered.append(st)
                if cur is not None:
                    cur["answer"] = (cur["answer"] + "\n" + st).strip()
                    # an auto/routing boilerplate reply is not a real answer
                    cur["answered"] = "auto/routing" not in st and "(none" not in st
            elif st:
                if cur is not None:
                    cur["body"] = (cur["body"] + "\n" + st).strip()
        if cur:
            asks.append(cur)
    ans = hitl / "answer.md"
    pending_text = ans.read_text(encoding="utf-8", errors="ignore").strip() \
        if ans.exists() else ""
    pending = bool(pending_text)
    reqs = []
    rdir = hitl / "requirements"
    if rdir.is_dir():
        # chronological (injection time), not alphabetical — matches the engine
        for d in sorted(rdir.iterdir(), key=lambda p: p.stat().st_mtime):
            if d.is_dir():
                body = ""
                rf = d / "REQUIREMENT.md"
                if rf.exists():
                    body = rf.read_text(encoding="utf-8", errors="ignore")
                try:
                    rt = (rf.stat().st_mtime if rf.exists() else d.stat().st_mtime)
                except OSError:
                    rt = 0.0
                reqs.append({"name": d.name, "body": body, "t": rt})
    # human→worker messages sent from the dashboard, with read status. A message
    # is "unread" only while it is still the staged answer.md (not yet consumed
    # by a worker or poll_note); once answer.md no longer holds it, it was read.
    sent = []
    sf = hitl / "sent.jsonl"
    if sf.is_file():
        for line in sf.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except Exception:        # noqa: BLE001 — skip a torn line
                continue
            txt = str(e.get("text", "")).strip()
            e["read"] = not (pending and txt == pending_text)
            sent.append(e)
    return {"asks": asks[-30:], "answered": answered[-30:],
            "pending": pending, "requirements": reqs, "sent": sent[-30:]}


# ── server ───────────────────────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    run_dir_override: pathlib.Path | None = None

    def _run_dir(self) -> pathlib.Path | None:
        return self.run_dir_override or _latest_run()

    def _run_admin(self, path: str, body: dict):
        """Select a run for viewing (pins run_dir_override) or archive one.
        Delete is a reversible move into runs-out/_archive/, never an
        on-disk erase, and is refused for a live or currently-pinned run."""
        raw = body.get("run") or body.get("dir") or ""
        # an empty run on select clears the pin → follow the live/latest run
        if path == "/api/run/select" and not str(raw).strip():
            _H.run_dir_override = None
            return self._send(200, "application/json",
                              b'{"ok":true,"selected":null}')
        target = _resolve_run(raw)
        if target is None:
            return self._send(400, "application/json",
                              b'{"ok":false,"error":"unknown run"}')
        if path == "/api/run/select":
            _H.run_dir_override = target
            return self._send(200, "application/json",
                              json.dumps({"ok": True, "selected": target.name}).encode())
        # delete → archive
        if _pid_alive(target):
            return self._send(409, "application/json",
                              b'{"ok":false,"error":"run is alive"}')
        if _H.run_dir_override and _H.run_dir_override.resolve() == target.resolve():
            return self._send(409, "application/json",
                              b'{"ok":false,"error":"run is selected"}')
        arch = OUT_DIR / "_archive"
        arch.mkdir(exist_ok=True)
        dest = arch / target.name
        i = 2
        while dest.exists():
            dest = arch / f"{target.name}__dup{i}"
            i += 1
        shutil.move(str(target), str(dest))
        return self._send(200, "application/json",
                          json.dumps({"ok": True, "archived": target.name}).encode())

    def do_GET(self):                                              # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        if parsed.path == "/api/state":
            rd = self._run_dir()
            st = _build_state(rd) if rd else {"empty": True}
            if rd is not None:
                # tell the frontend whether this run is pinned by an explicit
                # selection (vs. just following the live/latest run)
                st["pinned"] = _H.run_dir_override is not None
            payload = json.dumps(st, ensure_ascii=False).encode("utf-8")
            return self._send(200, "application/json; charset=utf-8", payload)
        if parsed.path == "/api/compare":
            # run-comparison table (П5): every run's metrics as JSON for
            # the Сравнение tab — the frontend filters/sorts client-side
            try:
                import compare_runs as cr
                runs = sorted((d for d in cr.RUNS.iterdir()
                               if d.is_dir() and "__" in d.name),
                              key=lambda d: d.name)
                rows = [cr.metrics(d) for d in runs]
            except Exception as exc:            # noqa: BLE001
                rows = [{"error": str(exc)}]
            payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
            return self._send(200, "application/json; charset=utf-8", payload)
        if parsed.path == "/api/idle":
            # duration / idle analysis for the ⏱ tab — per-operation gaps with
            # an attributed cause, plus roll-ups by node / phase / cause
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps(_idle_analysis(self._run_dir()),
                                         ensure_ascii=False).encode("utf-8"))
        if parsed.path == "/api/hitl":
            # bidirectional HITL state for the ✋ tab: the worker→human asks
            # (questions.md), whether an answer is still pending (answer.md
            # present = not yet consumed), and the late requirements already
            # injected. The human replies / injects via do_POST below.
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps(_hitl_state(self._run_dir()),
                                         ensure_ascii=False).encode("utf-8"))
        if parsed.path == "/api/file":
            rd = self._run_dir()
            rel = urllib.parse.parse_qs(parsed.query).get("path", [""])[0]
            if rd is None or not rel:
                return self._send(404, "text/plain", b"not found")
            target = (rd / rel).resolve()
            if rd.resolve() not in target.parents and target != rd.resolve():
                return self._send(403, "text/plain", b"forbidden")
            if not target.exists():
                return self._send(404, "text/plain", b"missing")
            text = target.read_text(encoding="utf-8", errors="ignore")
            fmt = urllib.parse.parse_qs(parsed.query).get("fmt", [""])[0]
            if fmt == "md":
                return self._send(200, "text/html; charset=utf-8",
                                  _md_to_html(text).encode("utf-8"))
            return self._send(200, "text/plain; charset=utf-8", text.encode("utf-8"))
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):                                             # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        # run select/delete act on a run named in the body — independent of
        # the currently-shown run, so they run before the active-run guard.
        if parsed.path in ("/api/run/select", "/api/run/delete"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                              # noqa: BLE001
                return self._send(400, "application/json",
                                  b'{"ok":false,"error":"bad json"}')
            try:
                return self._run_admin(parsed.path, body)
            except Exception as exc:                       # noqa: BLE001
                return self._send(500, "application/json",
                                  json.dumps({"ok": False, "error": str(exc)}).encode())
        rd = self._run_dir()
        if rd is None:
            return self._send(404, "application/json",
                              b'{"ok":false,"error":"no active run"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                  # noqa: BLE001
            return self._send(400, "application/json",
                              b'{"ok":false,"error":"bad json"}')
        try:
            if parsed.path == "/api/hitl/answer":
                # human → worker: write answer.md; the worker's ask loop polls
                # it, consumes it (non-empty) and unlinks. Mirrors the file the
                # cron operator writes by hand.
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._send(400, "application/json",
                                      b'{"ok":false,"error":"empty answer"}')
                (rd / "hitl").mkdir(parents=True, exist_ok=True)
                (rd / "hitl" / "answer.md").write_text(text + "\n",
                                                       encoding="utf-8")
                # Log the sent message so the chat shows it IMMEDIATELY as
                # "unread"; _hitl_state flips it to "read" once a worker (or
                # poll_note) consumes answer.md.
                try:
                    with open(rd / "hitl" / "sent.jsonl", "a",
                              encoding="utf-8") as _fh:
                        _fh.write(json.dumps({"t": round(_time.time(), 3),
                                              "text": text}) + "\n")
                except OSError:
                    pass
                return self._send(200, "application/json",
                                  b'{"ok":true,"wrote":"answer.md"}')
            if parsed.path == "/api/run/stop":
                # cooperative stop (П1): drop the STOP sentinel the engine
                # checks at each node boundary, then SIGTERM the run's pid
                # (run.pid) so a blocked worker wakes. The run exits with a
                # partial result a --resume can continue.
                import signal
                ws = rd / "workspace"
                (ws / ".spec-flow").mkdir(parents=True, exist_ok=True)
                (ws / ".spec-flow" / "STOP").write_text("stop\n",
                                                        encoding="utf-8")
                killed = None
                pidf = rd / "run.pid"
                if pidf.exists():
                    try:
                        pid = int(pidf.read_text().strip())
                        os.kill(pid, signal.SIGTERM)
                        killed = pid
                    except (OSError, ValueError):
                        killed = None
                return self._send(200, "application/json",
                                  json.dumps({"ok": True, "stopped": True,
                                              "signalled": killed}).encode())
            if parsed.path == "/api/run/start":
                # launch a fresh run (or --resume RUN_DIR) as a detached
                # process so it outlives this request. case defaults to the
                # latest run's leading slug token (p4-b2b… → p4).
                import subprocess
                import tempfile
                here = pathlib.Path(__file__).resolve().parent
                slug = str(body.get("case", "")).strip()
                if not slug:
                    latest = _latest_run()
                    tail = latest.name.split("__")[-1] if latest else "p4"
                    slug = tail.split("-")[0] or "p4"
                resume = str(body.get("resume", "")).strip()
                cmd = [sys.executable, str(here / "run_cases.py"),
                       "--case", slug, "--workers", "real",
                       "--depth", "execute", "--hitl", "auto"]
                if resume:
                    cmd += ["--resume", resume]
                # a live run still holding its pid blocks a fresh start
                if not resume:
                    cur = _latest_run()
                    pf = cur / "run.pid" if cur else None
                    if pf and pf.exists():
                        try:
                            os.kill(int(pf.read_text().strip()), 0)
                            return self._send(409, "application/json",
                                              b'{"ok":false,"error":"a run is already active"}')
                        except (OSError, ValueError):
                            pass
                logf = pathlib.Path(tempfile.gettempdir()) / \
                    f"specflow_dashboard_{slug}.log"
                fh = open(logf, "ab")
                subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                 cwd=str(here), start_new_session=True)
                return self._send(200, "application/json",
                                  json.dumps({"ok": True, "started": slug,
                                              "log": str(logf)}).encode())
            if parsed.path == "/api/hitl/inject":
                # human → engine: a late requirement. One folder per
                # requirement under hitl/requirements/<name>/REQUIREMENT.md,
                # the same artifact the engine scans mid-run (a scope line
                # '@scope: <nid>' may head the body).
                name = re.sub(r"[^\w-]+", "_", str(body.get("name", ""))).strip("_")
                text = str(body.get("text", "")).strip()
                if not name or not text:
                    return self._send(400, "application/json",
                                      b'{"ok":false,"error":"name and text required"}')
                d = rd / "hitl" / "requirements" / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "REQUIREMENT.md").write_text(text + "\n", encoding="utf-8")
                return self._send(200, "application/json",
                                  json.dumps({"ok": True, "injected": name}).encode())
        except Exception as exc:                           # noqa: BLE001
            return self._send(500, "application/json",
                              json.dumps({"ok": False, "error": str(exc)}).encode())
        return self._send(404, "application/json", b'{"ok":false}')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_PAGE = r"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<title>spec-flow live</title><style>
*{box-sizing:border-box}
/* SINGLE source of truth for every colour. Nothing below may use a raw hex
   literal — always reference a token. Status hues are deliberately ONE each:
   --err (red) and --ok (green) look identical everywhere they appear. */
:root{
 /* surfaces */
 --bg:#0d1117; --bg-sunken:#0f141a; --panel:#161b22; --panel-2:#21262d;
 --border:#30363d; --hair:#13171d;
 /* text */
 --fg:#c9d1d9; --fg-strong:#adbac7; --dim:#8b949e; --dim-2:#6e7681; --on-accent:#fff;
 /* brand / links */
 --accent:#1f6feb; --accent-hi:#388bfd; --link:#58a6ff; --link-2:#79c0ff; --purple:#a371f7;
 --code:#ffa657;
 /* status: error (red) — one hue, fills derived. -soft is the translucent
    box/row fill (softer, more pleasant than the solid -bg). */
 --err:#f85149; --err-bg:#4a1414; --err-bg-hi:#5a1d22;
 --err-soft:color-mix(in srgb,var(--err) 25%,transparent);
 /* status: ok / fixed (green) — one hue, fills derived */
 --ok:#3fb950; --ok-bg:#12361f; --ok-bg-hi:#163a25; --ok-border:#2f5d40;
 --ok-soft:color-mix(in srgb,var(--ok) 25%,transparent);
 /* status: warn (amber) */
 --warn:#e3b341; --warn-bg:#3a2a12;
 /* blue fills: selection + info panels */
 --sel-bg:#15324a; --sel-bg-hi:#1b3d59; --info-bg:#16202c; --info-bg-hi:#1d2c3d;
}
body{margin:0;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--bg);color:var(--fg)}
.bar{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:8px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.st{font-weight:700}.dim{color:var(--dim)}.pill{background:var(--panel-2);border-radius:10px;padding:1px 8px;cursor:pointer}
.home{cursor:pointer;background:var(--accent);color:var(--on-accent);border-radius:6px;padding:2px 10px;font-weight:700}.home:hover{background:var(--accent-hi)}
.runbar{display:flex;gap:8px;align-items:center;margin-left:auto;padding-left:14px;border-left:1px solid var(--border)}
.runbtn{cursor:pointer;border-radius:6px;padding:2px 10px;font-weight:700;border:1px solid var(--border)}
#runstop{background:var(--err-bg);color:var(--err)}#runstop:hover{background:var(--err-bg-hi)}
#runstart{background:var(--ok-bg);color:var(--ok)}#runstart:hover{background:var(--ok-bg-hi)}
#runlive{background:var(--info-bg);color:var(--link)}#runlive:hover{background:var(--info-bg-hi)}
.cmp tbody tr[data-run]{cursor:pointer}.cmp tbody tr[data-run]:hover{background:var(--panel)}
.cmp tr.selrow{background:var(--sel-bg)}.cmp tr.selrow:hover{background:var(--sel-bg-hi)}
.cmpact{text-align:center}.delrun{cursor:pointer;color:var(--dim)}.delrun:hover{color:var(--err)}
.runbtn.off{opacity:.35;cursor:not-allowed;pointer-events:none;filter:grayscale(.6)}
.live{color:var(--ok)}.donec{color:var(--dim)}
.bar2{position:sticky;top:38px;z-index:4;background:var(--bg-sunken);border-bottom:1px solid var(--panel-2);padding:3px 14px;display:block;font-size:12px}
.goal{color:var(--warn);display:block;min-height:1.2em;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cur{color:var(--ok);font-weight:700;display:block;margin-top:0;min-height:1.2em;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
ol.tl{padding-left:18px}ol.tl li{margin:1px 0;white-space:nowrap}
.tl .tk{color:var(--dim-2);display:inline-block;min-width:34px}
.tl .ph{color:var(--link-2);display:inline-block;min-width:96px}
.tl li:last-child{background:var(--ok-bg);border-radius:4px;padding:0 4px}
.wrap{display:grid;grid-template-columns:var(--treew,340px) 6px 1fr;gap:0;height:calc(100vh - 70px)}
.split{cursor:col-resize;background:var(--panel-2);position:relative}.split:hover{background:var(--accent)}
.split .knob{position:absolute;top:8px;left:-7px;width:20px;height:20px;line-height:20px;text-align:center;cursor:pointer;background:var(--panel);border:1px solid var(--border);border-radius:5px;color:var(--dim);font-size:11px;z-index:5}.split .knob:hover{color:var(--link)}
body.treecol .col.tree{overflow:hidden;padding:0;min-width:0}body.treecol .split .knob{left:1px}
body.treecol .wrap{grid-template-columns:0 6px 1fr}
.col{overflow:auto;height:100%}
.tree{padding:10px 8px;border-right:1px solid var(--panel-2)}
.tree ul{list-style:none;margin:0;padding-left:16px}
.tree li{margin:1px 0;white-space:nowrap}/* a wrapped label left the twisty alone on its own line — a fake empty row before long ids */
.node{cursor:pointer;padding:1px 6px;border-radius:5px;white-space:nowrap}
.node:hover{background:var(--panel)}.node.sel{background:color-mix(in srgb,var(--accent) 20%,transparent);outline:1px solid var(--accent)}
.tw{cursor:pointer;display:inline-block;width:12px;color:var(--dim)}
.badge{font-size:11px}
.detail{padding:12px 18px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0 10px;border-bottom:1px solid var(--panel-2);position:sticky;top:-12px;background:var(--bg);z-index:6;padding-top:12px}
.tab{cursor:pointer;padding:4px 10px;border:1px solid var(--border);border-bottom:none;border-radius:6px 6px 0 0;background:var(--panel);color:var(--dim)}
.tab.on{background:var(--bg);color:var(--link);border-color:var(--accent)}
h2{color:var(--link);border-bottom:1px solid var(--panel-2);padding-bottom:4px}h3,h4{color:var(--link-2)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
th,td{border:1px solid var(--border);padding:4px 7px;text-align:left;vertical-align:top}
th{background:var(--panel)}tr:nth-child(even) td{background:var(--bg-sunken)}
/* event-health rows: must come AFTER nth-child so the cell colour wins */
tr.evbad td{background:var(--err-soft)}tr.evfix td{background:var(--ok-soft)}
/* SINGLE source of pinned table headers. Any table wrapped in .cmpscroll keeps
   its header row fixed while the body scrolls vertically (model: the compare
   tab). Reused everywhere — compare, Простои, таймлайн, воркфлоу, события — so
   the look is defined in exactly ONE place. The opaque th background is what
   stops body rows from showing through the pinned header. */
.cmpscroll{max-height:calc(100vh - 230px);overflow:auto;border:1px solid var(--panel-2);border-radius:6px}
.cmpscroll table{margin:0}
.cmpscroll thead th{position:sticky;top:0;z-index:3;background:var(--panel)}
code{background:var(--panel);padding:1px 5px;border-radius:4px;color:var(--code)}
pre.code{background:var(--panel);padding:10px;border-radius:6px;overflow:auto;white-space:pre-wrap}
.feed{padding-left:38px;max-height:150px;overflow:auto}.feed.full{max-height:none;overflow:visible}.feed li{margin:1px 0}
.diff .add{background:var(--ok-bg);color:var(--ok)}.diff .del{background:var(--err-bg);color:var(--err)}
.muted{color:var(--dim-2)}.kv{color:var(--dim)}
.badge{font-size:9px;vertical-align:middle;letter-spacing:1px}
.actv{color:var(--warn);font-weight:700}
.errbox{background:var(--err-soft);border:1px solid color-mix(in srgb,var(--err) 40%,transparent);border-radius:6px;padding:6px 10px;margin:8px 0;font-size:12px}.errbox li{margin:2px 0}
.fixbox{background:var(--ok-soft);border:1px solid color-mix(in srgb,var(--ok) 40%,transparent);border-radius:6px;padding:6px 10px;margin:8px 0;font-size:12px}.fixbox li{margin:2px 0}
.gtabs{margin-top:8px}
</style></head><body>
<div class=bar>
 <b id=name class=home title="клик — вернуться к обзору">…</b>
 <span class=st id=status></span>
 <span class=dim id=counts></span>
 <span class=pill id=mode title="клик — вкл/выкл авторефреш"></span>
 <span class=runbar>
  <span class=dim id=runmsg></span>
  <span class=runbtn id=runstop title="кооперативная остановка прогона (STOP + SIGTERM)">⏹ стоп</span>
  <span class=runbtn id=runstart title="запустить новый прогон того же кейса">▶ ран</span>
  <span class=runbtn id=runlive title="открепить выбранный прогон — показывать живой/последний" style="display:none">⟲ к живому</span>
 </span>
</div>
<div class=bar2>
 <div id=goal class=goal></div>
 <div id=current class=cur></div>
</div>
<div class=wrap>
 <div class="col tree" id=tree></div>
 <div class=split id=split><span class=knob id=treeknob title="свернуть/развернуть панель">◀</span></div>
 <div class="col detail" id=detail></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
if(window.mermaid)mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'loose',flowchart:{useMaxWidth:false}});
// left panel: drag-resize + collapse (persisted)
(function(){
 const root=document.documentElement, body=document.body;
 const saved=localStorage.getItem('treew');
 if(saved)root.style.setProperty('--treew',saved);
 const knob=document.getElementById('treeknob');
 function setCollapsed(on){
  body.classList.toggle('treecol',on);
  knob.textContent=on?'▶':'◀';
  localStorage.setItem('treecol',on?'1':'');
 }
 setCollapsed(localStorage.getItem('treecol')==='1');
 knob.addEventListener('click',e=>{e.stopPropagation();
  setCollapsed(!body.classList.contains('treecol'));});
 const split=document.getElementById('split');
 let drag=false;
 split.addEventListener('mousedown',e=>{
  if(e.target===knob)return;
  drag=true;e.preventDefault();body.style.userSelect='none';});
 window.addEventListener('mousemove',e=>{
  if(!drag)return;
  const w=Math.max(120,Math.min(window.innerWidth-300,e.clientX));
  root.style.setProperty('--treew',w+'px');
  if(body.classList.contains('treecol'))setCollapsed(false);
 });
 window.addEventListener('mouseup',()=>{
  if(!drag)return;drag=false;body.style.userSelect='';
  localStorage.setItem('treew',
   getComputedStyle(root).getPropertyValue('--treew').trim());
 });
})();
let STATE=null, SEL=null, EXPANDED={}, GCOLL={}, CLICKT=null, NTAB='spec', GTAB='inputs', FILECACHE={}, AUTO=true;
let ACTIVE=[];
const isActive=id=>ACTIVE.some(a=>a.node===id);
function trackActive(){
 const arr=(STATE&&STATE.actives)||(STATE&&STATE.active?[STATE.active]:[]);
 const now=Date.now();
 // the server knows each call's REAL age — resync on every fetch so an
 // F5 (or a long-lived tab) never restarts the counters from zero
 ACTIVE=arr.map(a=>{const prev=ACTIVE.find(x=>x.node===a.node&&x.role===a.role);
  return {node:a.node,role:a.role,
          since:a.elapsed_s!=null?now-a.elapsed_s*1000:(prev?prev.since:now)};});}
function updElapsed(){const el=document.getElementById('elapsed');
 if(!el)return;
 // with auto-refresh OFF the data is frozen — a ticking counter would lie
 if(!AUTO){el.textContent='';return;}
 if(ACTIVE.length)el.textContent=' · уже '+ACTIVE.map(a=>fmtDur((Date.now()-a.since)/1000)).join(' / ');}
setInterval(updElapsed,1000);
const $=s=>document.querySelector(s);
// one duration format everywhere: <60s -> '42s', then 'MM:SS', with hours 'H:MM:SS'
function fmtDur(s){s=Math.max(0,Math.round(s));
 if(s<60)return s+'с';
 const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60,p=n=>String(n).padStart(2,'0');
 return h?h+':'+p(m)+':'+p(sec):p(m)+':'+p(sec);}
function mray(){if(window.mermaid){try{mermaid.run({querySelector:'#detail .mermaid'});}catch(e){}}}

async function poll(){
 if(!AUTO)return;
 try{const r=await fetch('/api/state');STATE=await r.json();render(false);
   // re-fetch the open lazily-loaded tab so it stays LIVE (not just re-rendered
   // from a stale cache): HITL chat status, idle analysis of the running case.
   if(!SEL && GTAB==='hitl') loadHitl();
   else if(!SEL && GTAB==='idle') loadIdle();
 }catch(e){}
}
function focusInside(el){const a=document.activeElement;
 return a&&el&&el.contains(a)&&/^(SELECT|INPUT|TEXTAREA|OPTION)$/.test(a.tagName);}
function withScroll(el,fn){if(!el){fn();return;}
 const top=el.scrollTop,left=el.scrollLeft;
 // also preserve the scroll of any inner ".keepscroll" panes (HITL chat, flow
 // timeline): match them by id before/after fn so an auto-refresh rebuild does
 // not jump them back to the top while the user is reading mid-scroll.
 const keep={};
 el.querySelectorAll('.keepscroll[id]').forEach(k=>{keep[k.id]={t:k.scrollTop,l:k.scrollLeft};});
 fn();
 el.scrollTop=top;el.scrollLeft=left;
 el.querySelectorAll('.keepscroll[id]').forEach(k=>{const s=keep[k.id];
  if(s){k.scrollTop=s.t;k.scrollLeft=s.l;}});}
const BADGE_ICON={spike:'🔬',clarify:'❓',contract:'📐',drift:'🌀',hitl:'✋',review_fails:'⚖️',error:'❌',pruned:'✂️',reworked:'🔧'};
const BADGE_TIP={spike:'было исследование (spike) перед решением',clarify:'было уточнение',contract:'есть контракт',drift:'зафиксирован дрейф',hitl:'вмешивался человек',review_fails:'ревью не прошло',error:'НЕзакрытая ошибка — подробности на странице узла',pruned:'дубль отрезан dedup-гейтом',reworked:'был REJECT — доработан, повторное ревью PASS'};
function badgeStr(eps){return (eps||[]).map(e=>BADGE_ICON[e]||'').join('');}
function badgeTips(eps){return (eps||[]).map(e=>(BADGE_ICON[e]||'')+' '+(BADGE_TIP[e]||e)).join('\n');}
function badgeHTML(eps){return (eps||[]).map(e=>BADGE_ICON[e]?`<span title="${BADGE_TIP[e]||e}">${BADGE_ICON[e]}</span>`:'').join('');}
function legendHTML(){return '<p class=dim style="font-size:11px;margin:4px 0">значки: ❌ незакрытая ошибка · 🔧 доработан после REJECT (итог PASS) · ✂️ дубль отрезан · 🔬 исследование · ✋ человек вмешивался · наведите на значок — подсказка</p>';}

function treeHTML(n){
 const has=n.children&&n.children.length;
 const open=EXPANDED[n.id]!==false; // default expanded
 const tw=has?`<span class=tw data-tw="${n.id}">${open?'▾':'▸'}</span>`:'<span class=tw></span>';
 const sel=SEL===n.id?' sel':'';
 const act=isActive(n.id);
 const ico=act?'⏳':(has?'🌿':'🍃');
 const pin=n.technical?'<span title="служебный узел движка (чекпойнт/гейт/сборка) — не вброс человека">⚙️</span> ':(n.attached?'<span title="позднее требование, вброшено в прогон">📌</span> ':'');
 const pend=n.pending?'<span title="вброшено, ещё не материализовано в узел" style="color:var(--warn)">⏳вброшено</span> ':'';
 let h=`<li>${tw}<span class="node${sel}${act?' actv':''}" data-id="${n.id}">${ico} ${pin}${pend}${n.id} <span class=badge>${badgeHTML(n.episodes)}</span></span>`;
 if(has&&open){h+='<ul>'+n.children.map(treeHTML).join('')+'</ul>';}
 h+='</li>';return h;
}

function render(user){
 if(user===undefined)user=true;            // explicit false only from the auto poll
 if(!STATE||STATE.empty){$('#status').textContent='нет прогонов';$('#detail').innerHTML='<p class=dim>runs-out пуст</p>';return;}
 const live=STATE.status!=='done';
 $('#status').innerHTML=live?'<span class=live>🟢 идёт…</span>':'<span class=donec>✅ завершён</span>';
 $('#name').textContent=STATE.name;
 const c=STATE.counts;$('#counts').textContent=`узлов ${c.nodes} · листьев ${c.leaves} · реализовано ${c.impl} · событий ${c.events}`;
 $('#mode').innerHTML='⟳ авто: '+(AUTO?'<span class=live>вкл</span>':'<span class=dim>выкл</span>');
 // stop/run availability follows the run's ACTUAL process liveness: a live
 // run can be stopped (not started); no live run can be started (not stopped)
 const active=!!STATE.run_active;
 const bs=$('#runstop'),br=$('#runstart');
 if(bs){bs.classList.toggle('off',!active);bs.title=active?'остановить активный прогон (STOP + SIGTERM)':'нет активного прогона';}
 if(br){br.classList.toggle('off',active);br.title=active?'прогон уже идёт — сначала останови':'запустить новый прогон того же кейса';}
 // the "back to live" button shows only while a specific run is pinned
 const bl=$('#runlive');if(bl)bl.style.display=STATE.pinned?'':'none';
 $('#goal').textContent=STATE.goal?('🎯 '+STATE.goal):'';
 trackActive();
 // the line is ALWAYS rendered (finished runs show the final state) — an emptied div collapses and the header jumps between 1 and 2 lines
 $('#current').innerHTML='сейчас: '+esc(STATE.current||'…')+'<span id=elapsed class=dim></span>';
 // fill the chip IMMEDIATELY — re-rendering recreated it empty and it stayed blank until the next 1s tick (visible blinking)
 updElapsed();
 // the tree updates live (new nodes appear) but must never yank the scroll
 withScroll($('#tree'),()=>{$('#tree').innerHTML='<ul>'+treeHTML(STATE.tree)+'</ul>';});
 // Every tab auto-updates. The ONLY thing that pauses a rebuild is the user
 // actively typing/selecting in a control (focusInside) — otherwise the rebuild
 // is non-disruptive: scroll is preserved (withScroll + .keepscroll), and
 // collapse/sort/selection live in persistent vars (EXPANDED, *_SORT, *_CASE),
 // so nothing is reset. No tab is ever "frozen" (which could leave it stale
 // forever without a user action).
 const det=$('#detail');
 if(!user && focusInside(det))return;
 withScroll(det,()=>{ if(!SEL) renderGlobal(); else renderNode(); });
}

function timelineHTML(){
 const t=STATE.timeline||[];
 if(!t.length)return '<p class=dim>событий ещё нет…</p>';
 const ts=t.map(e=>e.t).filter(x=>x!=null);const t0=ts.length?Math.min(...ts):0;
 const rows=t.slice().reverse().map(e=>{
  const rel=e.t!=null?('+'+fmtDur(e.t-t0)):'—';
  return `<tr><td>${rel}</td><td>${e.tick??''}</td><td><span class=ph>${esc(e.phase)}</span></td><td>${esc(e.text)}</td><td>${e.verdict?('<b>'+esc(e.verdict)+'</b>'):''}</td></tr>`;
 }).join('');
 return '<p class=muted>сверху — последние по времени; «время» = от старта прогона; '+
  '«соб.№» — номер события в полном журнале (тут только вехи, поэтому номера с пропусками)</p>'+
  '<div class=cmpscroll style="max-height:calc(100vh - 180px)"><table><thead><tr><th>время</th><th title="номер события в полном журнале прогона">соб.№</th><th>фаза</th><th>действие</th><th>вердикт</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

// run comparison (П5): lazy-load /api/compare once, filter+sort client-side
let CMP=null, CMP_CASE='', CMP_SORT='started', CMP_DESC=true;
const CMP_COLS=[['started','запуск'],['run','прогон'],['duration','время'],['ticks','тиков'],
 ['tree_nodes','узлов'],['llm_calls','LLM'],['reviews_rejected','реворк'],
 ['demotions','демоц'],['crashes','краш'],['leaf_timeouts','таймаут'],
 ['integrate_pass','интегр✓'],['integrate_red','интегрR'],
 ['quota_waits','квота'],['auto_answers','авто'],['reqs_attached','требов'],
 ['root_red','корень'],
 // normalized efficiency (size-independent) — compare runs of different power
 ['calls_per_node','LLM/узел'],['errors_per_node','ошиб/узел'],
 ['rework_per_node','реворк/узел'],['sec_per_node','сек/узел'],
 ['first_pass_rate','1й-пасс'],['quota_per_call','квота/LLM'],
 ['tokens_total','токенов'],['tokens_per_node','ток/узел']];
// columns where LOWER is better (defect/cost density) vs HIGHER is better
const CMP_LOWER=new Set(['calls_per_node','errors_per_node','rework_per_node',
 'sec_per_node','quota_per_call','tokens_per_node']);
const CMP_HIGHER=new Set(['first_pass_rate']);
const CMP_NORM=new Set([...CMP_LOWER,...CMP_HIGHER]);
// short tooltips (<=2 sentences) for the abbreviated headers
const CMP_TIPS={
 run:'Номер прогона (vNNN). Берётся из имени каталога runs-out.',
 duration:'Длительность от первого до последнего события трассы. «~» = прогон не финиширован (живой или оборван).',
 ticks:'Число тиков — внутренних шагов движка в трассе. Грубая мера объёма проделанной работы.',
 tree_nodes:'Сколько узлов в дереве спеков. У живого прогона считается из трассы (tree.json пишется только в конце).',
 llm_calls:'Сколько раз воркеры обращались к модели (call_start). Прямая мера расхода LLM.',
 reviews_rejected:'Сколько раз ревьюер отклонил спеку (REJECT) → доработка. Высокое число = спеки рождаются сырыми.',
 demotions:'Бездетная ветка низведена до листа: ветка без детей прошла бы пустой-зелёной, движок заставляет её реализовать.',
 crashes:'Сколько листьев упали с ошибкой воркера. Лист сдаётся красным, прогон продолжается.',
 leaf_timeouts:'Сколько листьев превысили потолок времени и сданы красными. Защита от зависшего узла.',
 integrate_pass:'Сколько интеграционных гейтов прошли зелёными (ветка собрана и проверена).',
 integrate_red:'Сколько раз интеграция дала красный набор тестов на первой проверке (до починки).',
 quota_waits:'Сколько раз прогон ждал сброса квоты вместо смерти. Высокое = free-пул жёстко лимитирован.',
 auto_answers:'Сколько вопросов воркеров закрыл автоответчик по известной политике (без 5-мин ожидания человека).',
 reqs_attached:'Сколько поздних требований движок материализовал в дерево (ATTACHED).',
 root_red:'Корневая интеграция дала FAIL: прогон дошёл до конца, но собранный продукт НЕ зелёный.',
 calls_per_node:'Удельный расход: обращений к модели на один узел дерева. Меньше = экономнее. Сравнимо между кейсами разной мощности.',
 errors_per_node:'Плотность дефектов: (реворк+интегрR+краш+таймаут) на узел. Меньше = чище идёт прогон.',
 rework_per_node:'Доработок ревью на узел. Меньше = спеки рождаются зрелее.',
 sec_per_node:'Пропускная способность: секунд стенных часов на узел. Меньше = быстрее.',
 first_pass_rate:'Доля интеграций, зелёных с ПЕРВОЙ попытки = интегр✓/(интегр✓+интегрR). Больше = выше качество сборки.',
 quota_per_call:'Давление квоты: ожиданий квоты на один LLM-вызов. Меньше = свободнее пул.',
 tokens_total:'Реальные токены за прогон (prompt+completion из API usage). 0 = провайдер не вернул usage.',
 tokens_per_node:'Удельный расход токенов на узел. Меньше = экономнее. Сравнимо между кейсами.',
 started:'Когда прогон был запущен (из имени папки). По умолчанию сортировка — новые сверху.'};
function loadCompare(){
 fetch('/api/compare').then(r=>r.json()).then(d=>{CMP=d;render();})
  .catch(()=>{CMP=[{error:'не удалось загрузить'}];render();});
}
function compareHTML(){
 if(CMP===null){loadCompare();return '<p class=dim>загружаю сравнение…</p>';}
 if(CMP.length&&CMP[0].error)return '<p class=dim>ошибка: '+esc(CMP[0].error)+'</p>';
 const cases=[...new Set(CMP.map(r=>(r.dir||'').split('__').pop()))].sort();
 let rows=CMP.filter(r=>!CMP_CASE||(r.dir||'').endsWith(CMP_CASE));
 // the «запуск» column shows a human stamp but sorts on the epoch ts
 const sk=CMP_SORT==='started'?'started_ts':CMP_SORT;
 rows.sort((a,b)=>{let x=a[sk],y=b[sk];
  if(typeof x==='boolean'){x=x?1:0;y=y?1:0;}
  if(x<y)return CMP_DESC?1:-1;if(x>y)return CMP_DESC?-1:1;return 0;});
 const opts=['<option value="">все кейсы</option>'].concat(
  cases.map(c=>`<option value="${esc(c)}"${CMP_CASE===c?' selected':''}>${esc(c)}</option>`)).join('');
 let h='<p class=muted>сравнение прогонов — фильтр по кейсу, клик по заголовку = сортировка, '+
  'клик по строке = показать этот прогон на дашборде, 🗑 = в архив. '+
  '«~» у времени = прогон не финиширован. Данные из tests/compare_runs.py</p>';
 h+='<div style="margin:8px 0"><label>кейс: <select id=cmpcase>'+opts+'</select></label> '+
  '<span class="tab" id=cmpreload style="margin-left:8px">↻ обновить</span> '+
  '<span class=dim>'+rows.length+' прогон(ов)</span></div>';
 // only the table BODY scrolls: the filter row + header stay put, the
 // data area gets its own scroll box sized to the remaining pane height
 // per-column min/max over the SHOWN rows, for relative heat-coloring of
 // the normalized efficiency columns (best=green, worst=red)
 const ext={};
 CMP_NORM.forEach(k=>{const xs=rows.map(r=>r[k]).filter(v=>typeof v==='number');
  if(xs.length)ext[k]=[Math.min(...xs),Math.max(...xs)];});
 const heat=(k,v)=>{if(typeof v!=='number'||!ext[k])return '';
  const[mn,mx]=ext[k];if(mn===mx)return '';
  const t=(v-mn)/(mx-mn);const good=CMP_LOWER.has(k)?(1-t):t; // 1=best
  if(good>=0.66)return ' style="color:var(--ok)"';
  if(good<=0.33)return ' style="color:var(--err)"';
  return ' style="color:var(--warn)"';};
 const curName=(STATE&&STATE.name)||'';
 h+='<div class=cmpscroll><table class=cmp><thead><tr>'+CMP_COLS.map(([k,t])=>
  `<th data-sort="${k}" title="${esc(CMP_TIPS[k]||'')}" style="cursor:help"${CMP_NORM.has(k)?' class=normcol':''}>${t}${CMP_SORT===k?(CMP_DESC?' ▾':' ▴'):''}</th>`).join('')+'<th title="перенести прогон в архив"></th></tr></thead><tbody>';
 rows.forEach(r=>{
  const sel=(r.dir&&r.dir===curName)?' class=selrow':'';
  h+=`<tr data-run="${esc(r.dir||'')}" title="клик — показать этот прогон"${sel}>`+CMP_COLS.map(([k])=>{
  let v=r[k];if(typeof v==='boolean')v=v?'<b style="color:var(--err)">RED</b>':'—';
  const hc=CMP_NORM.has(k)?heat(k,r[k]):'';
  return `<td${hc}>${v===undefined?'':v}</td>`;}).join('')+
  `<td class=cmpact><span class=delrun data-del="${esc(r.dir||'')}" title="в архив (runs-out/_archive/)">🗑</span></td></tr>`;});
 h+='</tbody></table></div>';
 return h;
}

// duration / idle analysis (⏱): where the wall-clock went, by op + cause
let IDLE=null, IDLE_SORT='dur', IDLE_DESC=true;
function loadIdle(){
 fetch('/api/idle').then(r=>r.json()).then(d=>{IDLE=d;if(GTAB==='idle')render(false);})
  .catch(()=>{IDLE={error:'не удалось загрузить'};render(false);});
}
function fmtDur(s){if(s>=60)return (s/60).toFixed(1)+'м';return s.toFixed(0)+'с';}
function idleHTML(){
 if(IDLE===null){loadIdle();return '<p class=dim>анализирую длительности…</p>';}
 if(IDLE.error)return '<p class=dim>ошибка: '+esc(IDLE.error)+'</p>';
 if(IDLE.empty)return '<p class=dim>нет активного прогона</p>';
 const cols=[['tick','тик'],['node','узел'],['phase','фаза'],['action','операция'],
  ['cause','причина'],['dur','длит']];
 let rows=(IDLE.top||[]).slice();
 rows.sort((a,b)=>{let x=a[IDLE_SORT],y=b[IDLE_SORT];
  if(x<y)return IDLE_DESC?1:-1;if(x>y)return IDLE_DESC?-1:1;return 0;});
 // cause heat: quota waits + integration are the usual long poles
 const causeColor=c=>({'ожидание квоты':'var(--err)','интеграция (тесты)':'var(--warn)',
  'починка':'var(--code)','реализация (LLM)':'var(--link)','ревью спеки':'var(--link-2)',
  'декомпозиция (LLM)':'var(--purple)','ответ человека':'var(--code)'}[c]||'var(--dim)');
 let h='<h3 class=muted>⏱ Анализ простоев и длительности '+
  '<span class="tab" id=idlereload style="margin-left:8px">↻ обновить</span></h3>';
 h+='<p class=muted>суммарно учтено <b>'+fmtDur(IDLE.total_s||0)+'</b> по '+
  (IDLE.events||0)+' операциям. Длительность = разрыв до следующего события трассы, '+
  'причина атрибутирована по фазе/действию и окнам ожидания квоты.</p>';
 // headline usage per LLM (provider+model): requests, tokens in/out, wall time
 const au=IDLE.agent_usage||{rows:[],totals:{}};
 if(au.rows.length){
  const aut=au.totals||{};
  h+='<h4>Использование ЛЛМ/агентов <span class=dim>(запросы · токены вход/выход · время)</span></h4>';
  h+='<div class=cmpscroll style="max-height:300px"><table class=cmp><thead><tr>'+
   '<th>провайдер</th><th>модель</th><th>запросов</th><th>токены вход</th>'+
   '<th>токены выход</th><th>всего токенов</th><th>время</th><th>сбоев</th></tr></thead><tbody>';
  au.rows.forEach(r=>{
   h+=`<tr><td>${esc(r.provider)}</td><td>${esc(r.model)}</td><td>${r.requests}</td>`+
    `<td>${r.prompt}</td><td>${r.completion}</td>`+
    `<td><b>${r.total}</b>${r.estimated?' <span class=dim>≈</span>':''}</td>`+
    `<td>${fmtDur(r.time_s)}</td>`+
    `<td style="color:${r.abnormal?'var(--err)':'inherit'}">${r.abnormal||0}</td></tr>`;});
  h+='</tbody></table></div>';
  h+=`<p class=dim>итого: <b>${aut.requests||0}</b> запросов · <b>${aut.tokens||0}</b> токенов · <b>${fmtDur(aut.time_s||0)}</b></p>`;
 }
 // roll-up by cause — where the time structurally goes
 h+='<h4>По причинам (куда уходит время)</h4><div class=cmpscroll style="max-height:none">'+
  '<table class=cmp><thead><tr><th>причина</th><th>суммарно</th><th>операций</th><th>запросов LLM</th><th>доля</th></tr></thead><tbody>';
 // share = each cause's part of the SUM of cause times, so the column adds up
 // to ~100%. (Dividing by total_s was wrong: LLM causes report real call time
 // from the llm-log while total_s sums trace-gap wall time — different clocks,
 // so the biggest row read as 100% and the rest as fractions of it.)
 const causeSum=(IDLE.by_cause||[]).reduce((s,r)=>s+(r.total||0),0)||1;
 (IDLE.by_cause||[]).forEach(r=>{const sh=Math.round(100*(r.total||0)/causeSum);
  h+=`<tr><td style="color:${causeColor(r.cause)}">${esc(r.cause)}</td>`+
   `<td>${fmtDur(r.total)}</td><td>${r.count}</td><td>${r.llm||0}</td>`+
   `<td><span style="display:inline-block;height:8px;background:${causeColor(r.cause)};width:${sh}px;max-width:120px"></span> ${sh}%</td></tr>`;});
 h+='</tbody></table></div>';
 // FAILURE report — abnormal responses → fallback / error, with the delay they
 // cost. Only shown when something actually went wrong (a clean run hides it).
 const fl=IDLE.failures||{by_model:[],transitions:[],totals:{}};
 const ft=fl.totals||{};
 if((ft.abnormal||0)>0||(ft.fallbacks||0)>0){
  h+='<h4 style="color:var(--err)">Сбои моделей → фоллбек/ошибка</h4>';
  h+='<p class=muted>модель не ответила нормально <b>'+(ft.abnormal||0)+'</b> раз → '+
   '<b>'+(ft.fallbacks||0)+'</b> фоллбеков, <b style="color:var(--err)">'+(ft.terminal||0)+
   '</b> терминальных ошибок; потеряно на ретраях/ожидании <b>'+fmtDur(ft.delay_s||0)+'</b>.</p>';
  h+='<div class=cmpscroll style="max-height:240px"><table class=cmp><thead><tr>'+
   '<th>модель</th><th>сбоев</th><th>429</th><th>5xx</th><th>timeout</th><th>пусто</th>'+
   '<th>фоллбеков</th><th>ошибок</th><th>задержка</th></tr></thead><tbody>';
  (fl.by_model||[]).forEach(r=>{ if(!(r.abn||r.fallbacks))return;
   h+=`<tr><td>${esc(r.model)}</td><td>${r.abn||0}</td><td>${r['429']||0}</td>`+
    `<td>${r['5xx']||0}</td><td>${r.timeout||0}</td><td>${r.empty||0}</td>`+
    `<td>${r.fallbacks||0}</td><td style="color:${r.errors?'var(--err)':'inherit'}">${r.errors||0}</td>`+
    `<td>${fmtDur(r.delay_s||0)}</td></tr>`;});
  h+='</tbody></table></div>';
  // transition chain: who fell back to whom and why
  const trs=(fl.transitions||[]);
  if(trs.length){
   h+='<p class=muted style="margin-top:6px">Переходы (что пробовали после сбоя):</p><div class=cmpscroll style="max-height:160px"><ul style="margin:2px 0 0 14px;padding:0">';
   trs.slice().reverse().forEach(t=>{
    const to=t.terminal||!t.to ? '<b style="color:var(--err)">✗ цепочка исчерпана</b>' : esc(t.to);
    h+=`<li><span class=dim>${esc(t.from||'—')}</span> →<span style="color:var(--code)">(${esc(t.reason||'?')})</span>→ ${to}</li>`;});
   h+='</ul></div>';
  }
 }
 // token economics by role+model (#6) — real spend, not call counts
 const tok=IDLE.tokens||{rows:[],total:0};
 if(tok.rows.length){
  h+='<h4>Токены по роли/специалисту+модели <span class=dim>(≈ = посчитано токенайзером, API не вернул usage)</span></h4><div class=cmpscroll style="max-height:240px">'+
   '<table class=cmp><thead><tr><th>стадия</th><th>роль/специалист</th><th>модель</th><th>вызовов</th><th>prompt</th><th>completion</th><th>всего</th><th>доля</th></tr></thead><tbody>';
  tok.rows.forEach(r=>{const sh=tok.total?Math.round(100*r.total/tok.total):0;
   h+=`<tr><td>${esc(r.stage||'—')}</td><td>${esc(r.role)}${r.estimated?' <span class=dim>≈</span>':''}</td><td>${esc(r.model)}</td><td>${r.calls}</td>`+
    `<td>${r.prompt}</td><td>${r.completion}</td><td><b>${r.total}</b></td>`+
    `<td><span style="display:inline-block;height:8px;background:var(--link);width:${sh}px;max-width:120px"></span> ${sh}%</td></tr>`;});
  h+=`</tbody></table></div><p class=dim>всего токенов: <b>${tok.total}</b></p>`;
 } else {
  // never vanish silently — say WHY the table is empty (a stopped/young run has
  // no token_usage events yet; a completed run repopulates it per specialist).
  h+='<h4>Токены по роли/специалисту+модели</h4>'+
   '<p class=dim>нет событий token_usage в текущем прогоне '+
   '(прогон ещё идёт или был остановлен рано — таблица заполнится по мере вызовов LLM).</p>';
 }
 // roll-up by node — which nodes cost the most
 h+='<h4>По узлам (самые дорогие)</h4><div class=cmpscroll style="max-height:260px">'+
  '<table class=cmp><thead><tr><th>узел</th><th>суммарно</th><th>операций</th></tr></thead><tbody>'+
  (IDLE.by_node||[]).map(r=>`<tr><td>${esc(r.node||'—')}</td><td>${fmtDur(r.total)}</td><td>${r.count}</td></tr>`).join('')+
  '</tbody></table></div>';
 // the longest individual operations — sortable
 h+='<h4>Самые длинные операции (клик по заголовку = сортировка)</h4>';
 h+='<div class=cmpscroll><table class=cmp><thead><tr>'+cols.map(([k,t])=>
  `<th data-isort="${k}" style="cursor:pointer">${t}${IDLE_SORT===k?(IDLE_DESC?' ▾':' ▴'):''}</th>`).join('')+'</tr></thead><tbody>';
 rows.forEach(r=>{h+='<tr>'+
  `<td>${r.tick??''}</td><td>${esc(r.node||'')}</td><td>${esc(r.phase||'')}</td>`+
  `<td title="${esc(r.action||'')}">${esc((r.action||'').slice(0,40))}</td>`+
  `<td style="color:${causeColor(r.cause)}">${esc(r.cause||'')}</td>`+
  `<td><b>${fmtDur(r.dur)}</b></td></tr>`;});
 h+='</tbody></table></div>';
 return h;
}

// bidirectional HITL (✋): worker→human asks + human→worker answer/inject
let HITL=null;
function loadHitl(){
 // render(false) → the auto-refresh guard keeps your scroll and never wipes the
 // answer box while you are typing in it (focus guard in render()).
 fetch('/api/hitl').then(r=>r.json()).then(d=>{HITL=d;if(GTAB==='hitl')render(false);})
  .catch(()=>{HITL={error:'не удалось загрузить'};render(false);});
}
function hitlHTML(){
 if(HITL===null){loadHitl();return '<p class=dim>загружаю HITL…</p>';}
 if(HITL.error)return '<p class=dim>ошибка: '+esc(HITL.error)+'</p>';
 if(HITL.empty)return '<p class=dim>нет активного прогона</p>';
 const asks=HITL.asks||[],ans=HITL.answered||[],reqs=HITL.requirements||[],sent=HITL.sent||[];
 const open=asks.filter(a=>a&&typeof a==='object'&&!a.answered).length;
 // TWO vertical panels: LEFT = input fields (answer + inject), RIGHT = chat
 // history that scrolls. A full-width header sits above the two-column row.
 let h='<div class=hitlwrap2>';
 h+='<h3 class=muted>✋ HITL — двусторонний канал с прогоном '+
  '<span class="tab" id=hitlreload style="margin-left:8px">↻ обновить</span></h3>';
 h+='<div class=hitlrow>';
 // ── LEFT PANEL: input fields ─────────────────────────────────────────
 h+='<div class=hitlcol-left>';
 h+='<div class='+(HITL.pending?'errbox':'fixbox')+'>'+
  (HITL.pending?'⏳ <b>ответ записан, воркер ещё не забрал</b> (answer.md ждёт потребления)'
   :'✓ <b>нет неотправленного ответа</b> — можно отвечать на новый вопрос')+
  (open?' · <span style="color:var(--err)">без ответа: '+open+'</span>':'')+'</div>';
 h+='<h4>Ответить воркеру (человек → worker)</h4>'+
  '<textarea id=hitlans rows=4 style="width:100%" placeholder="Текст ответа — попадёт в hitl/answer.md, воркер заберёт его из своего цикла ожидания"></textarea>'+
  '<div style="margin:6px 0"><span class="tab" id=hitlsend>➤ отправить ответ</span> '+
  '<span class=dim id=hitlmsg></span></div>';
 h+='<h4>Вбросить позднее требование (человек → движок)</h4>'+
  '<div class=dim>имя = папка под hitl/requirements/; первая строка тела может быть «@scope: &lt;узел&gt;»</div>'+
  '<input id=hitlname placeholder="имя требования (web_ui)" style="width:100%;margin:4px 0">'+
  '<textarea id=hitlreq rows=4 style="width:100%" placeholder="Текст требования (REQUIREMENT.md)"></textarea>'+
  '<div style="margin:6px 0"><span class="tab" id=hitlinject>➤ вбросить</span> '+
  '<span class=dim id=hitlimsg></span></div>';
 h+='</div>';  // .hitlcol-left
 // ── RIGHT PANEL: chat history (messenger-style, scrollable) ───────────
 // incoming (worker→human) on the left, outgoing (human→worker/engine) on
 // the right; meta header (time/sender/node) atop each bubble; unanswered
 // questions flagged red + ⏳.
 h+='<div class=hitlcol-right>';
 const chatCss=
   // outer column: full-width header + a two-panel row that fills the rest.
   '.hitlwrap2{display:flex;flex-direction:column;height:calc(100vh - 130px);min-height:340px}'+
   '.hitlwrap2>h3{flex:0 0 auto}'+
   '.hitlrow{flex:1 1 auto;min-height:0;display:flex;flex-direction:row;gap:14px}'+
   // LEFT: inputs, fixed share of the width, own scroll if tall.
   '.hitlcol-left{flex:0 0 40%;max-width:40%;display:flex;flex-direction:column;'+
   'overflow-y:auto;min-height:0;padding-right:6px}'+
   '.hitlcol-left>h4,.hitlcol-left>div,.hitlcol-left>input,.hitlcol-left>textarea{flex:0 0 auto}'+
   // RIGHT: chat column, the .chat fills it and scrolls.
   '.hitlcol-right{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;min-height:0}'+
   '.hitlcol-right>h4{flex:0 0 auto}'+
   '.chat{flex:1 1 auto;min-height:0;overflow-y:auto;padding:8px;margin-top:4px;'+
   'background:var(--bg);border:1px solid var(--panel);border-radius:8px;display:flex;flex-direction:column}'+
   '.bub{max-width:78%;margin:4px 0;padding:6px 9px;border-radius:12px;'+
   'white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.35}'+
   '.bin{align-self:flex-start;background:var(--border);border:1px solid var(--border);'+
   'border-bottom-left-radius:3px}'+
   '.bout{align-self:flex-end;background:var(--ok-bg);border:1px solid var(--ok-border);'+
   'border-bottom-right-radius:3px}'+
   '.bun{align-self:flex-start;background:var(--err-bg-hi);border:1px solid var(--err-bg-hi);'+
   'border-bottom-left-radius:3px}'+
   '.bmeta{font-size:11px;opacity:.7;margin-bottom:3px;font-weight:600}'+
   '.bun .bmeta{color:var(--err);opacity:1}';
 h+='<style>'+chatCss+'</style>';
 const meta=t=>`<div class=bmeta>${esc(t)}</div>`;
 const bub=(side,m,txt)=>`<div class="bub ${side}">${meta(m)}${esc(txt)}</div>`;
 // unified, TIME-SORTED post list. Each post carries an epoch `t`; the meta
 // line is prefixed with HH:mm. Posts with no known time sort to the top.
 const hm=t=>t?new Date(t*1000).toTimeString().slice(0,5):'--:--';
 const posts=[];
 asks.forEach(a=>{
   if(typeof a==='string'){posts.push({t:0,html:bub('bin','воркер',a)});return;}
   const unans=!a.answered, t=a.t||0;
   posts.push({t,html:`<div class="bub ${unans?'bun':'bin'}">`+
     meta(hm(t)+' · '+(unans?'⏳ без ответа · ':'')+'👷 '+(a.header||'воркер'))+
     esc(a.body||'')+'</div>'});
   if(a.answer)posts.push({t:t+0.001,html:bub('bout',hm(t)+' · 🧑 человек / авто',
     a.answer.replace(/^\*\*answer[^:]*:\*\*\s*/i,''))});
 });
 reqs.forEach(r=>{const t=r.t||0;
   posts.push({t,html:bub('bout',hm(t)+' · 📌 вброшено требование · '+(r.name||''),
     (r.body||'').slice(0,400))});});
 // standalone recorded answers not already shown
 (ans||[]).slice(-10).forEach(a=>{
   const txt=String(a).replace(/^\*\*answer[^:]*:\*\*\s*/i,'');
   if(!asks.some(x=>x&&x.answer&&x.answer.indexOf(txt)>=0)
      && !sent.some(s=>String(s.text||'').trim()===txt.trim()))
     posts.push({t:0,html:bub('bout','🧑 ответ (история)',txt)});
 });
 // direct human→worker messages with delivery status (from sent.jsonl): shown
 // the instant you send (⏳ не прочитано) and flipped to ✓ once a worker consumes it.
 sent.forEach(s=>{const t=s.t||0;
   posts.push({t,html:bub('bout',hm(t)+' · '+(s.read?'✓ прочитано воркером':'⏳ не прочитано — ждёт воркера')
             +' · 🧑 человек → worker', s.text||'')});});
 posts.sort((x,y)=>(x.t||0)-(y.t||0));
 const chat=posts.map(p=>p.html).join('');
 h+='<h4>История переписки ('+(asks.length+reqs.length+sent.length)+')</h4>';
 h+= chat? '<div class="chat keepscroll" id=hitlchat>'+chat+'</div>'
   : '<p class=dim>пока сообщений нет</p>';
 h+='</div>';  // .hitlcol-right
 h+='</div>';  // .hitlrow
 h+='</div>';  // .hitlwrap2
 return h;
}
function runCtl(path,payload){
 const el=$('#runmsg');if(el)el.textContent='…';
 fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(payload)}).then(r=>r.json()).then(d=>{
   if(el)el.textContent=d.ok?(d.stopped?'⏹ остановлен'+(d.signalled?' (pid '+d.signalled+')':''):'▶ запущен: '+esc(d.started||'')):('✗ '+(d.error||'ошибка'));
  }).catch(()=>{if(el)el.textContent='✗ сеть';});
}
// pin the dashboard to a chosen run (empty name = follow the live/latest run)
function runSelect(name){
 fetch('/api/run/select',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({run:name})}).then(()=>{SEL=null;poll();renderGlobal();})
  .catch(()=>{});
}
function hitlPost(path,payload,msgEl){
 fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(payload)}).then(r=>r.json()).then(d=>{
   const el=$(msgEl);if(el)el.textContent=d.ok?'✓ записано':('✗ '+(d.error||'ошибка'));
   if(d.ok)loadHitl();
  }).catch(e=>{const el=$(msgEl);if(el)el.textContent='✗ сеть';});
}

// flow tab (🔀): a client-side SVG with a vertical TIME axis. Events from
// STATE.timeline are placed by their epoch `t`; columns ("lanes") group by
// phase. Consecutive events of the SAME node are linked top-to-bottom so a
// node's progress reads down the page. The drawing height scales with the run
// duration so long runs scroll; the SVG lives in a .keepscroll container so an
// auto-refresh rebuild does not reset the scroll position.
function renderGlobal(){
 const R=STATE.reports;
 const tabs=[['inputs','▶ Старт (цель+вход)'],['graph','🕸 Граф спеков'],['flow','🔀 Поток выполнения'],['timeline','⏱ Таймлайн'],['report','Отчёт+аудит'],['agents','🤖 Агенты сейчас'],['hitl','✋ HITL'],['idle','⏱ Простои'],['compare','📊 Сравнение прогонов'],['workflow','Воркфлоу'],['oracle','Оракул'],['commits','Версии/коммиты'],['summary','Итог']];
 let h='<div class=tabs>'+tabs.map(([k,t])=>(k==='timeline'||k==='graph'||k==='agents'||k==='hitl'||k==='flow'||k==='idle'||k==='compare'||R[k])?`<span class="tab${GTAB===k?' on':''}" data-g="${k}">${t}</span>`:'').join('')+'</div>';
 let body;
 if(GTAB==='agents')body='<h3 class=muted>Что делают агенты сейчас <span class=dim>(сверху — последнее)</span></h3><ol class="feed full" reversed>'+(STATE.feed||[]).slice().reverse().map(f=>`<li>${esc(f)}</li>`).join('')+'</ol>';
 else if(GTAB==='hitl')body=hitlHTML();
 else if(GTAB==='flow')body='<p class=muted>поток выполнения по вертикальной шкале времени (сверху позже): колонки — ветки, блоки — вехи (цвет = вердикт), позиция = реальная метка времени</p><div id=flowscroll class=keepscroll style="overflow:auto;max-height:75vh">'+(R.flow||'<p class=dim>потока ещё нет</p>')+'</div>';
 else if(GTAB==='idle')body=idleHTML();
 else if(GTAB==='compare')body=compareHTML();
 else if(GTAB==='timeline')body=timelineHTML();
 else if(GTAB==='graph')body='<p class=muted>граф задач, что построил плагин — <b>дабл-клик</b> = провалиться в спеку/код/версии · <b>клик</b> = свернуть поддерево / развернуть следующий уровень. 🌿 ветка · 🍃 лист · бейджи = эпизоды</p>'+graphSVG();
 // воркфлоу: server-rendered markdown (may hold tables) — scroll body, pin headers
 else if(GTAB==='workflow')body='<div class=cmpscroll style="max-height:calc(100vh - 170px)">'+(R.workflow||'<p class=dim>нет данных</p>')+'</div>';
 else body=R[GTAB]||'<p class=dim>нет данных</p>';
 h+='<div id=gbody>'+body+'</div>';
 $('#detail').innerHTML=h;
 mray();
}

function effKids(n){return GCOLL[n.id]?[]:(n.children||[]);}
function countDesc(n){return (n.children||[]).reduce((a,c)=>a+1+countDesc(c),0);}
function graphSVG(){
 const root=STATE.tree;if(!root)return '';let row=0;
 (function assign(n,d){n._d=d;const ch=effKids(n);if(!ch.length){n._y=row++;}else{ch.forEach(c=>assign(c,d+1));n._y=(ch[0]._y+ch[ch.length-1]._y)/2;}})(root,0);
 const COLW=200,ROWH=30,PX=14,PY=14,BW=164,BH=22;let maxD=0,maxY=0;const nodes=[],edges=[];
 (function walk(n){maxD=Math.max(maxD,n._d);maxY=Math.max(maxY,n._y);nodes.push(n);effKids(n).forEach(c=>{edges.push([n,c]);walk(c);});})(root);
 const X=n=>PX+n._d*COLW,Y=n=>PY+n._y*ROWH;const W=PX*2+(maxD+1)*COLW,H=PY*2+(maxY+1)*ROWH;
 let s=`<svg width="${W}" height="${H}" style="min-width:${W}px">`;
 edges.forEach(([a,b])=>{const x1=X(a)+BW,y1=Y(a)+BH/2,x2=X(b),y2=Y(b)+BH/2;s+=`<path d="M${x1} ${y1} C${x1+24} ${y1}, ${x2-24} ${y2}, ${x2} ${y2}" stroke="var(--border)" fill="none"/>`;});
 nodes.forEach(n=>{const leaf=!(n.children&&n.children.length);const sel=SEL===n.id;const coll=!leaf&&GCOLL[n.id];const act=isActive(n.id);
  const ico=act?'⏳':(coll?'▸🌿':(leaf?'🍃':'🌿'));
  const tail=coll?` +${countDesc(n)}`:'';
  const bdg=badgeStr(n.episodes);
  const tip=badgeTips(n.episodes);
  s+=`<g class=gnode data-id="${n.id}" transform="translate(${X(n)},${Y(n)})" style="cursor:pointer">`+
     (tip?`<title>${esc(tip)}</title>`:'')+
     `<rect width="${BW}" height="${BH}" rx="5" fill="${n.pending?'color-mix(in srgb,var(--warn) 30%,transparent)':(act?'color-mix(in srgb,var(--warn) 18%,transparent)':(sel?'color-mix(in srgb,var(--accent) 33%,transparent)':(leaf?'var(--panel)':'var(--ok-bg)')))}" stroke="${n.pending?'var(--warn)':(act?'var(--warn)':(sel?'var(--accent)':(leaf?'var(--border)':'var(--ok)')))}"${(act||n.pending)?' stroke-dasharray="4 3"':''}/>`+
     `<text x="7" y="15" fill="var(--fg)" font-size="11">${ico} ${n.pending?'⏳':(n.technical?'⚙️':(n.attached?'📌':''))}${esc(n.id).slice(0,14)}${tail}</text>`+
     (bdg?`<text x="${BW-5}" y="14" text-anchor="end" font-size="8">${bdg}</text>`:'')+`</g>`;});
 s+='</svg>';return '<div style="overflow:auto;border:1px solid var(--panel-2);border-radius:6px;padding:6px">'+s+'</div>'+legendHTML();
}

// SINGLE source of truth for event health (used by BOTH the node summary
// boxes and the events table). Never duplicate this classification: a verdict
// is BAD if REJECT/FAIL/ERROR; it is 'fixed' when a later event (by tick) on
// the SAME gate passed, otherwise 'open'. Returns '' | 'open' | 'fixed'.
function evBad(v){return ['REJECT','FAIL','ERROR'].includes(String(v));}
function evStatus(e,all){
 if(!evBad(e.verdict))return '';
 const g=e.gate,t=Number(e.tick)||0;
 return (g&&all.some(x=>x.gate===g&&String(x.verdict)==='PASS'&&(Number(x.tick)||0)>t))?'fixed':'open';
}

function renderNode(){
 const nd=STATE.nodes[SEL];if(!nd){SEL=null;return renderGlobal();}
 const f=nd.files||{};
 const tabs=[['spec','Спека',f.spec],['versions',`Версии (${(f.versions||[]).length+ (f.spec?1:0)})`,f.spec||f.versions.length],['code','Код',f.code],['test','Тест',f.test],['contract','Контракт',f.contract],['events',`События (${nd.events.length})`,true]];
 let h=`<h2>${SEL} <span class=badge>${badgeHTML(nd.episodes)}</span></h2>`;
 h+=`<div class=kv>вердикт: <b>${nd.verdict}</b> · уровень: L${nd.depth} · родитель: ${nd.parent||'—'}`;
 const m=nd.metrics||{};if(Object.keys(m).length)h+=` · LOC≈${m.estimated_loc??'?'} · задач ${m.tasks??'?'} · решений ${m.open_decisions??'?'}`;
 // 'versions' counts SPEC re-authorings only; integrate-time repairs live
 // in events — surface them so a red node never looks 'untouched'
 const nrep=(nd.events||[]).filter(e=>/rework|repair/i.test(e.action||'')).length;
 if(nrep)h+=` · <span title="раунды доработки спеки + раунды починки интеграции (события rework/repair)">попыток починки: ${nrep}</span>`;
 h+=`</div>`;
 // a bad verdict followed by a later PASS on the SAME gate is fixed history,
 // not a live problem — show the two groups apart so badges and boxes agree
 const evs=nd.events||[];
 const open=evs.filter(e=>evStatus(e,evs)==='open');
 const fixed=evs.filter(e=>evStatus(e,evs)==='fixed');
 if(open.length){
  h+='<div class=errbox><b>❌ незакрытые проблемы ('+open.length+'):</b><ul>'+
   open.map(e=>`<li><span class=dim>#${e.tick??''}</span> <b>${esc(e.gate||e.phase||'')}</b> → ${esc(e.verdict)}: ${esc(e.detail||e.action||'')}</li>`).join('')+
   '</ul></div>';
 }
 if(fixed.length){
  h+='<div class=fixbox><b>🔧 исправлено доработкой ('+fixed.length+') — повторная проверка PASS:</b><ul>'+
   fixed.map(e=>`<li><span class=dim>#${e.tick??''}</span> <b>${esc(e.gate||e.phase||'')}</b> было ${esc(e.verdict)}: ${esc(e.detail||e.action||'')}</li>`).join('')+
   '</ul></div>';
 }
 h+='<div class=tabs><span class="tab" data-n="__back">⬅ обзор</span>'+tabs.map(([k,t,on])=>on?`<span class="tab${NTAB===k?' on':''}" data-n="${k}">${t}</span>`:'').join('')+'</div>';
 h+='<div id=nbody>загрузка…</div>';
 $('#detail').innerHTML=h;
 renderNodeBody(nd);
}

async function getFile(p){if(!p)return '';if(FILECACHE[p]!=null)return FILECACHE[p];const r=await fetch('/api/file?path='+encodeURIComponent(p));const t=await r.text();FILECACHE[p]=t;return t;}
async function getMD(p){if(!p)return '';const k='md:'+p;if(FILECACHE[k]!=null)return FILECACHE[k];const r=await fetch('/api/file?path='+encodeURIComponent(p)+'&fmt=md');const t=await r.text();FILECACHE[k]=t;return t;}

async function renderNodeBody(nd){
 const f=nd.files||{},b=$('#nbody');if(!b)return;
 if(NTAB==='spec'){b.innerHTML=await getMD(f.spec);}
 else if(NTAB==='code'){b.innerHTML='<pre class=code>'+esc(await getFile(f.code))+'</pre>';}
 else if(NTAB==='test'){b.innerHTML='<pre class=code>'+esc(await getFile(f.test))+'</pre>';}
 else if(NTAB==='contract'){b.innerHTML='<pre class=code>'+esc(await getFile(f.contract))+'</pre>';}
 else if(NTAB==='events'){
   // newest first (descending event number). A FAIL/REJECT/ERROR row is marked
   // by OUTCOME: 🔧 amber = later resolved (a PASS on the SAME gate came after);
   // ❌ red = still OPEN (no later PASS on that gate). So the table itself shows
   // WHERE a problem occurred and whether a rework closed it.
   const all=(nd.events||[]);
   const evs=[...all].sort((a,b)=>(Number(b.tick)||0)-(Number(a.tick)||0));
   // Row colour comes from evStatus(): RED (.evbad) = still-open problem,
   // GREEN (.evfix) = resolved by a later PASS on the same gate. The colour
   // alone tells WHERE it broke and whether a rework closed it.
   b.innerHTML='<div class=dim style="margin:4px 0">🟥 незакрытая · 🟩 исправлена доработкой</div>'+
   '<div class=cmpscroll style="max-height:calc(100vh - 200px)"><table><thead><tr><th title="номер события в полном журнале">соб.№ ↓</th><th>фаза</th><th>роль</th><th>LLM/агент</th><th>действие</th><th>гейт</th><th>вердикт</th><th>детали</th></tr></thead><tbody>'+
   evs.map(e=>{const st=evStatus(e,all);const cls=st==='open'?'evbad':(st==='fixed'?'evfix':'');const mark=st==='open'?'❌ ':(st==='fixed'?'🔧 ':'');return `<tr${cls?(' class='+cls):''}><td>${e.tick??''}</td><td>${esc(e.phase)}</td><td>${esc(e.profile)}</td><td>${esc(e.model||'')}</td><td>${esc(e.action)}</td><td>${esc(e.gate)}</td><td>${mark}${e.verdict?('<b>'+esc(e.verdict)+'</b>'):''}</td><td>${esc(e.detail||'')}</td></tr>`;}).join('')+'</tbody></table></div>';
 }
 else if(NTAB==='versions'){
   // ordered: v1..vN (superseded) then current spec = newest
   const vers=(f.versions||[]).slice();const all=vers.concat(f.spec?[f.spec]:[]);
   if(all.length<=1){b.innerHTML='<p class=dim>ревизий не было — одна версия спеки</p>'+(await getMD(f.spec));return;}
   const texts=await Promise.all(all.map(getFile));
   const htmls=await Promise.all(all.map(getMD));
   let h='<p class=muted>история ревизий узла (старое → новое); diff = изменения относительно предыдущей версии</p>';
   for(let i=0;i<all.length;i++){
     const label=all[i].includes('.v')?all[i].match(/\.v(\d+)\./)[0].replace(/\./g,''):'current';
     const finding=(texts[i].match(/supersed(?:es|ed_by)[^\n]*|finding[^\n]*/i)||[''])[0];
     h+=`<h4>${label} <span class=muted>${esc(all[i])}</span></h4>`;
     if(finding)h+=`<p class=muted>причина: ${esc(finding)}</p>`;
     if(i>0){h+='<div class=diff>'+lineDiff(texts[i-1],texts[i])+'</div>';}
     else{h+='<div class=mdwrap>'+htmls[i]+'</div>';}
   }
   b.innerHTML=h;
 }
}

function lineDiff(a,b){
 const A=a.split('\n'),B=b.split('\n');const setA=new Set(A),setB=new Set(B);let h='<pre class=code>';
 B.forEach(l=>{if(!setA.has(l))h+='<div class=add>+ '+esc(l)+'</div>';});
 A.forEach(l=>{if(!setB.has(l))h+='<div class=del>- '+esc(l)+'</div>';});
 if(h==='<pre class=code>')h+='<span class=muted>(текст совпадает)</span>';
 return h+'</pre>';
}

function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}

document.addEventListener('click',e=>{
 if(e.target.closest('#name')){SEL=null;render();return;}
 if(e.target.closest('#mode')){AUTO=!AUTO;if(AUTO)poll();else render();return;}
 const tw=e.target.closest('.tw[data-tw]');
 if(tw){const id=tw.dataset.tw;EXPANDED[id]=EXPANDED[id]===false?true:false;render();return;}
 const nodeEl=e.target.closest('.node[data-id]');
 if(nodeEl){SEL=nodeEl.dataset.id;NTAB='spec';render();return;}
 const gn=e.target.closest('.gnode[data-id]');
 if(gn){const id=gn.dataset.id;clearTimeout(CLICKT);CLICKT=setTimeout(()=>toggleGraph(id),260);return;}
 const sorth=e.target.closest('th[data-sort]');
 if(sorth){const k=sorth.dataset.sort;if(CMP_SORT===k)CMP_DESC=!CMP_DESC;else{CMP_SORT=k;CMP_DESC=false;}renderGlobal();return;}
 const del=e.target.closest('[data-del]');
 if(del){const n=del.dataset.del;
  if(confirm('Перенести прогон '+n+' в архив? (runs-out/_archive/, обратимо)'))
   fetch('/api/run/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({run:n})}).then(r=>r.json()).then(d=>{
    if(!d.ok)alert('Не удалось: '+(d.error||'?'));CMP=null;poll();renderGlobal();});
  return;}
 const rr=e.target.closest('tr[data-run]');
 if(rr&&rr.dataset.run){runSelect(rr.dataset.run);return;}
 if(e.target.closest('#runlive')){runSelect('');return;}
 const g=e.target.closest('[data-g]');if(g){GTAB=g.dataset.g;renderGlobal();return;}
 const rl=e.target.closest('#cmpreload');if(rl){CMP=null;renderGlobal();return;}
 if(e.target.closest('#runstop')){
  if(confirm('Остановить активный прогон? (STOP + SIGTERM, можно продолжить через --resume)'))
   runCtl('/api/run/stop',{});return;}
 if(e.target.closest('#runstart')){
  if(confirm('Запустить новый прогон того же кейса?'))runCtl('/api/run/start',{});return;}
 if(e.target.closest('#idlereload')){IDLE=null;loadIdle();return;}
 const isort=e.target.closest('[data-isort]');
 if(isort){const k=isort.dataset.isort;if(IDLE_SORT===k)IDLE_DESC=!IDLE_DESC;
  else{IDLE_SORT=k;IDLE_DESC=true;}renderGlobal();return;}
 if(e.target.closest('#hitlreload')){HITL=null;loadHitl();return;}
 if(e.target.closest('#hitlsend')){const t=($('#hitlans')||{}).value||'';
  if(t.trim())hitlPost('/api/hitl/answer',{text:t},'#hitlmsg');
  else $('#hitlmsg').textContent='✗ пусто';return;}
 if(e.target.closest('#hitlinject')){const nm=($('#hitlname')||{}).value||'',
  t=($('#hitlreq')||{}).value||'';
  if(nm.trim()&&t.trim())hitlPost('/api/hitl/inject',{name:nm,text:t},'#hitlimsg');
  else $('#hitlimsg').textContent='✗ имя и текст обязательны';return;}
 const nt=e.target.closest('[data-n]');if(nt){if(nt.dataset.n==='__back'){SEL=null;render();}else{NTAB=nt.dataset.n;renderNode();}return;}
});

function findNode(n,id){if(n.id===id)return n;for(const c of n.children||[]){const r=findNode(c,id);if(r)return r;}return null;}
function toggleGraph(id){
 const n=STATE&&STATE.tree?findNode(STATE.tree,id):null;
 if(!n||!(n.children&&n.children.length))return;     // leaves have nothing to fold
 if(GCOLL[id]){
   // collapsed -> expand ONE level: children appear, deeper levels stay folded
   delete GCOLL[id];
   (n.children||[]).forEach(c=>{if(c.children&&c.children.length)GCOLL[c.id]=true;});
 }else{
   GCOLL[id]=true;                                   // expanded -> fold whole subtree
 }
 renderGlobal();
}
document.addEventListener('change',e=>{
 if(e.target.id==='cmpcase'){CMP_CASE=e.target.value;renderGlobal();}
});
document.addEventListener('dblclick',e=>{
 const gn=e.target.closest('.gnode[data-id]');
 if(gn){clearTimeout(CLICKT);SEL=gn.dataset.id;NTAB='spec';render();}
});

poll();setInterval(poll,REFRESH_MS_VAL);
</script></body></html>"""
_PAGE = _PAGE.replace("REFRESH_MS_VAL", str(REFRESH_MS))


def start_in_thread(port: int = 8088, run_dir: str | None = None) -> ThreadingHTTPServer:
    """Start the dashboard server in a daemon thread and return it.

    Used by the run entrypoint when launched with ``--dashboard`` so the page is
    up for the whole run. ``run_dir=None`` auto-follows the most recent run (i.e.
    the one being created), so it tracks live progress without being pinned.
    """
    import threading
    if run_dir:
        _H.run_dir_override = pathlib.Path(run_dir).resolve()
    srv = ThreadingHTTPServer(("0.0.0.0", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description="Interactive live/offline run dashboard")
    ap.add_argument("--run-dir", help="run folder to watch (default: latest in runs-out)")
    ap.add_argument("--port", type=int, default=8088)
    args = ap.parse_args()
    if args.run_dir:
        _H.run_dir_override = pathlib.Path(args.run_dir).resolve()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), _H)
    where = args.run_dir if args.run_dir else "latest run (auto-follow)"
    print(f"dashboard → http://localhost:{args.port}   watching: {where}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
