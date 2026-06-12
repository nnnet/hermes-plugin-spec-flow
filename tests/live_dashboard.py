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
import time as _time
import pathlib
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import spec_flow_tools as T                                       # noqa: E402

OUT_DIR = ROOT / "tests" / "runs-out"
REFRESH_MS = 2000


# ── helpers ──────────────────────────────────────────────────────────────────
def _latest_run() -> pathlib.Path | None:
    dirs = [p for p in OUT_DIR.iterdir() if p.is_dir()] if OUT_DIR.exists() else []
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


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
    return {
        "id": nid, "verdict": m.get("verdict", "leaf"),
        "episodes": m.get("episodes", []),
        "children": [_tree_view(c, meta) for c in node.get("children", []) or []],
    }


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


def _events_by_node(events: list[dict]) -> dict:
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


# ── starting inputs + service info (readable "what we start from") ───────────
def _kv_table(d: dict) -> str:
    rows = [f"| {html.escape(str(k))} | {html.escape(str(v))} |" for k, v in d.items()]
    return "| параметр | значение |\n|---|---|\n" + "\n".join(rows) if rows else ""


def _inputs_md(run_dir: pathlib.Path) -> str:
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
            body[lane].append(f"    style {nid} fill:#3a1620,stroke:#f85149")
        elif str(e.get("phase")) in ep_phase or e.get("gate") in ("clarify", "drift", "hitl"):
            body[lane].append(f"    style {nid} fill:#3a2a12,stroke:#e3b341")
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
            lines += body[lane]
            lines.append("  end")
    else:
        for lane in order:
            lines += [ln[2:] for ln in body[lane]]
    return "```mermaid\n" + "\n".join(lines) + "\n```"


# ── state ────────────────────────────────────────────────────────────────────
def _build_state(run_dir: pathlib.Path) -> dict:
    events = _read_jsonl(run_dir / "trace.jsonl")
    llm = _read_jsonl(run_dir / "llm-log.jsonl")
    ws = run_dir / "workspace"
    done = (run_dir / "SUMMARY.md").exists()

    tree = _tree_from_file(run_dir) or _tree_from_llm(llm)
    meta: dict = {}
    _flatten(tree, 0, meta, None)
    files = {nid: _node_files(ws, nid) for nid in meta} if ws.exists() else {}
    ev_idx = _events_by_node(events)

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
        elif e.get("event") == "outcome":
            open_calls.pop(key, None)
    last_start = list(open_calls.values())[-1] if open_calls else None
    _ROLE_RU = {"decomposer": "декомпозирует", "implementer": "пишет код",
                "reviewer": "ревьюит", "researcher": "исследует"}
    if not done:
        if open_calls:
            parts = []
            for e in open_calls.values():
                lvl = e.get("depth")
                chip = f" (L{lvl})" if isinstance(lvl, int) and lvl >= 0 else ""
                parts.append(f"{_ROLE_RU.get(e.get('role'), e.get('role'))}"
                             f" «{e.get('node')}»{chip}")
            current = "🟢 " + "  ⏐  ".join(parts)
        elif events:
            le = events[-1]
            current = f"🟢 {le.get('phase')}: {le.get('action')}"
    timeline = [{"tick": e.get("tick"), "t": e.get("t"), "phase": e.get("phase"),
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
            "inputs": _md_to_html(_inputs_md(run_dir)),
            "flow": _md_to_html(_flow_mermaid(events, tree)) if events else None,
            "report": report_html,
            "oracle": read_md("oracle-report.md"),
            "summary": read_md("SUMMARY.md"),
            "commits": read_md("workspace/COMMITS.md"),
            "workflow": read_md("workflow.md"),
        },
    }


# ── server ───────────────────────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    run_dir_override: pathlib.Path | None = None

    def _run_dir(self) -> pathlib.Path | None:
        return self.run_dir_override or _latest_run()

    def do_GET(self):                                              # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        if parsed.path == "/api/state":
            rd = self._run_dir()
            payload = json.dumps(_build_state(rd) if rd else {"empty": True},
                                 ensure_ascii=False).encode("utf-8")
            return self._send(200, "application/json; charset=utf-8", payload)
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
body{margin:0;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:#0d1117;color:#c9d1d9}
.bar{position:sticky;top:0;z-index:5;background:#161b22;border-bottom:1px solid #30363d;padding:8px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.st{font-weight:700}.dim{color:#8b949e}.pill{background:#21262d;border-radius:10px;padding:1px 8px;cursor:pointer}
.home{cursor:pointer;background:#1f6feb;color:#fff;border-radius:6px;padding:2px 10px;font-weight:700}.home:hover{background:#388bfd}
.live{color:#3fb950}.donec{color:#8b949e}
.bar2{position:sticky;top:38px;z-index:4;background:#0f141a;border-bottom:1px solid #21262d;padding:5px 14px;display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:12px}
.goal{color:#e3b341;display:block;min-height:1.25em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cur{color:#3fb950;font-weight:700;display:block;margin-top:2px;min-height:1.25em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
ol.tl{padding-left:18px}ol.tl li{margin:1px 0;white-space:nowrap}
.tl .tk{color:#6e7681;display:inline-block;min-width:34px}
.tl .ph{color:#79c0ff;display:inline-block;min-width:96px}
.tl li:last-child{background:#12361f;border-radius:4px;padding:0 4px}
.wrap{display:grid;grid-template-columns:340px 1fr;gap:0;height:calc(100vh - 70px)}
.col{overflow:auto;height:100%}
.tree{padding:10px 8px;border-right:1px solid #21262d}
.tree ul{list-style:none;margin:0;padding-left:16px}
.tree li{margin:1px 0}
.node{cursor:pointer;padding:1px 6px;border-radius:5px;white-space:nowrap}
.node:hover{background:#161b22}.node.sel{background:#1f6feb33;outline:1px solid #1f6feb}
.tw{cursor:pointer;display:inline-block;width:12px;color:#8b949e}
.badge{font-size:11px}
.detail{padding:12px 18px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0 10px;border-bottom:1px solid #21262d}
.tab{cursor:pointer;padding:4px 10px;border:1px solid #30363d;border-bottom:none;border-radius:6px 6px 0 0;background:#161b22;color:#8b949e}
.tab.on{background:#0d1117;color:#58a6ff;border-color:#1f6feb}
h2{color:#58a6ff;border-bottom:1px solid #21262d;padding-bottom:4px}h3,h4{color:#79c0ff}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
th,td{border:1px solid #30363d;padding:4px 7px;text-align:left;vertical-align:top}
th{background:#161b22}tr:nth-child(even) td{background:#0f141a}
code{background:#161b22;padding:1px 5px;border-radius:4px;color:#ffa657}
pre.code{background:#161b22;padding:10px;border-radius:6px;overflow:auto;white-space:pre-wrap}
.feed{padding-left:38px;max-height:150px;overflow:auto}.feed li{margin:1px 0}
.diff .add{background:#12361f;color:#3fb950}.diff .del{background:#3a1620;color:#f85149}
.muted{color:#6e7681}.kv{color:#8b949e}
.badge{font-size:9px;vertical-align:middle;letter-spacing:1px}
.actv{color:#e3b341;font-weight:700}
.errbox{background:#3a162040;border:1px solid #f8514966;border-radius:6px;padding:6px 10px;margin:8px 0;font-size:12px}.errbox li{margin:2px 0}
.fixbox{background:#163a1c40;border:1px solid #3fb95066;border-radius:6px;padding:6px 10px;margin:8px 0;font-size:12px}.fixbox li{margin:2px 0}
.gtabs{margin-top:8px}
</style></head><body>
<div class=bar>
 <b id=name class=home title="клик — вернуться к обзору">…</b>
 <span class=st id=status></span>
 <span class=dim id=counts></span>
 <span class=pill id=mode title="клик — вкл/выкл авторефреш"></span>
</div>
<div class=bar2>
 <div id=goal class=goal></div>
 <div id=current class=cur></div>
</div>
<div class=wrap>
 <div class="col tree" id=tree></div>
 <div class="col detail" id=detail></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
if(window.mermaid)mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'loose',flowchart:{useMaxWidth:false}});
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
setInterval(()=>{const el=document.getElementById('elapsed');
 if(!el)return;
 // with auto-refresh OFF the data is frozen — a ticking counter would lie
 if(!AUTO){el.textContent='';return;}
 if(ACTIVE.length)el.textContent=' · уже '+ACTIVE.map(a=>fmtDur((Date.now()-a.since)/1000)).join(' / ');},1000);
const $=s=>document.querySelector(s);
// one duration format everywhere: <60s -> '42s', then 'MM:SS', with hours 'H:MM:SS'
function fmtDur(s){s=Math.max(0,Math.round(s));
 if(s<60)return s+'с';
 const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60,p=n=>String(n).padStart(2,'0');
 return h?h+':'+p(m)+':'+p(sec):p(m)+':'+p(sec);}
function mray(){if(window.mermaid){try{mermaid.run({querySelector:'#detail .mermaid'});}catch(e){}}}

async function poll(){
 if(!AUTO)return;
 try{const r=await fetch('/api/state');STATE=await r.json();render();}catch(e){}
}
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
 let h=`<li>${tw}<span class="node${sel}${act?' actv':''}" data-id="${n.id}">${ico} ${n.id} <span class=badge>${badgeHTML(n.episodes)}</span></span>`;
 if(has&&open){h+='<ul>'+n.children.map(treeHTML).join('')+'</ul>';}
 h+='</li>';return h;
}

function render(){
 if(!STATE||STATE.empty){$('#status').textContent='нет прогонов';$('#detail').innerHTML='<p class=dim>runs-out пуст</p>';return;}
 const live=STATE.status!=='done';
 $('#status').innerHTML=live?'<span class=live>🟢 идёт…</span>':'<span class=donec>✅ завершён</span>';
 $('#name').textContent=STATE.name;
 const c=STATE.counts;$('#counts').textContent=`узлов ${c.nodes} · листьев ${c.leaves} · реализовано ${c.impl} · событий ${c.events}`;
 $('#mode').innerHTML='⟳ авто: '+(AUTO?'<span class=live>вкл</span>':'<span class=dim>выкл</span>');
 $('#goal').textContent=STATE.goal?('🎯 '+STATE.goal):'';
 trackActive();
 // the line is ALWAYS rendered (finished runs show the final state) — an emptied div collapses and the header jumps between 1 and 2 lines
 $('#current').innerHTML='сейчас: '+esc(STATE.current||'…')+'<span id=elapsed class=dim></span>';
 $('#tree').innerHTML='<ul>'+treeHTML(STATE.tree)+'</ul>';
 if(!SEL) renderGlobal(); else renderNode();
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
  '<table><thead><tr><th>время</th><th title="номер события в полном журнале прогона">соб.№</th><th>фаза</th><th>действие</th><th>вердикт</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function renderGlobal(){
 const R=STATE.reports;
 const tabs=[['inputs','▶ Старт (цель+вход)'],['graph','🕸 Граф спеков'],['flow','🔀 Поток выполнения'],['timeline','⏱ Таймлайн'],['report','Отчёт+аудит'],['workflow','Воркфлоу'],['oracle','Оракул'],['commits','Версии/коммиты'],['summary','Итог']];
 let h='<div class=tabs>'+tabs.map(([k,t])=>(k==='timeline'||k==='graph'||R[k])?`<span class="tab${GTAB===k?' on':''}" data-g="${k}">${t}</span>`:'').join('')+'</div>';
 h+='<h3 class=muted>Что делают агенты сейчас <span class=dim>(сверху — последнее)</span></h3><ol class=feed reversed>'+(STATE.feed||[]).slice().reverse().map(f=>`<li>${esc(f)}</li>`).join('')+'</ol>';
 let body;
 if(GTAB==='timeline')body=timelineHTML();
 else if(GTAB==='graph')body='<p class=muted>граф задач, что построил плагин — <b>дабл-клик</b> = провалиться в спеку/код/версии · <b>клик</b> = свернуть поддерево / развернуть следующий уровень. 🌿 ветка · 🍃 лист · бейджи = эпизоды</p>'+graphSVG();
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
 edges.forEach(([a,b])=>{const x1=X(a)+BW,y1=Y(a)+BH/2,x2=X(b),y2=Y(b)+BH/2;s+=`<path d="M${x1} ${y1} C${x1+24} ${y1}, ${x2-24} ${y2}, ${x2} ${y2}" stroke="#30363d" fill="none"/>`;});
 nodes.forEach(n=>{const leaf=!(n.children&&n.children.length);const sel=SEL===n.id;const coll=!leaf&&GCOLL[n.id];const act=isActive(n.id);
  const ico=act?'⏳':(coll?'▸🌿':(leaf?'🍃':'🌿'));
  const tail=coll?` +${countDesc(n)}`:'';
  const bdg=badgeStr(n.episodes);
  const tip=badgeTips(n.episodes);
  s+=`<g class=gnode data-id="${n.id}" transform="translate(${X(n)},${Y(n)})" style="cursor:pointer">`+
     (tip?`<title>${esc(tip)}</title>`:'')+
     `<rect width="${BW}" height="${BH}" rx="5" fill="${act?'#3a2a1255':(sel?'#1f6feb55':(leaf?'#161b22':'#13251a'))}" stroke="${act?'#e3b341':(sel?'#1f6feb':(leaf?'#30363d':'#3fb950'))}"${act?' stroke-dasharray="4 3"':''}/>`+
     `<text x="7" y="15" fill="#c9d1d9" font-size="11">${ico} ${esc(n.id).slice(0,14)}${tail}</text>`+
     (bdg?`<text x="${BW-5}" y="14" text-anchor="end" font-size="8">${bdg}</text>`:'')+`</g>`;});
 s+='</svg>';return '<div style="overflow:auto;border:1px solid #21262d;border-radius:6px;padding:6px">'+s+'</div>'+legendHTML();
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
 const isBad=e=>['REJECT','FAIL','ERROR'].includes(String(e.verdict));
 const fixedBy=i=>{const g=evs[i].gate;return g&&evs.some((x,j)=>j>i&&x.gate===g&&String(x.verdict)==='PASS');};
 const open=[],fixed=[];
 evs.forEach((e,i)=>{if(isBad(e))(fixedBy(i)?fixed:open).push(e);});
 if(open.length){
  h+='<div class=errbox><b>❌ незакрытые проблемы ('+open.length+'):</b><ul>'+
   open.map(e=>`<li><b>${esc(e.gate||e.phase||'')}</b> → ${esc(e.verdict)}: ${esc(e.detail||e.action||'')}</li>`).join('')+
   '</ul></div>';
 }
 if(fixed.length){
  h+='<div class=fixbox><b>🔧 исправлено доработкой ('+fixed.length+') — повторная проверка PASS:</b><ul>'+
   fixed.map(e=>`<li><b>${esc(e.gate||e.phase||'')}</b> было ${esc(e.verdict)}: ${esc(e.detail||e.action||'')}</li>`).join('')+
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
   b.innerHTML='<table><thead><tr><th title="номер события в полном журнале">соб.№</th><th>фаза</th><th>роль</th><th>действие</th><th>гейт</th><th>вердикт</th><th>детали</th></tr></thead><tbody>'+
   nd.events.map(e=>`<tr><td>${e.tick??''}</td><td>${esc(e.phase)}</td><td>${esc(e.profile)}</td><td>${esc(e.action)}</td><td>${esc(e.gate)}</td><td>${e.verdict?('<b>'+esc(e.verdict)+'</b>'):''}</td><td>${esc(e.detail||'')}</td></tr>`).join('')+'</tbody></table>';
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
 const g=e.target.closest('[data-g]');if(g){GTAB=g.dataset.g;renderGlobal();return;}
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
