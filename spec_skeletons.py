"""spec_skeletons — the ENGINE writes the module skeleton from the spec IR.

Node C1 of the spec-IR rearchitecture (plan 2026-07-04T00-45), Stage 17.

Why: every surface the model is free to restate is a surface it can drift
(v143 rename, v157 phantom import, v164 template drift). Stage 13 made the
node interface DATA (`spec_ir.build_ir`); this module removes the model's
freedom to restate it: the skeleton — signatures, allowed imports, env
access points, per-function contract anchors — is COMPILED from the IR,
the model fills ONLY the function bodies, and the write door judges every
delivery against the same IR entry.

What:
  * ``compile_skeleton(ir, node_id)`` — deterministic module skeleton text
    from the node's IR entry (empty string when the node has no interface);
  * ``skeleton_conformance(ir, node_id, code)`` — AST-based, attributable
    findings: rewritten signature, import outside ``symbols.consumes`` +
    stdlib, public function/class beyond the IR (closed world), stripped
    skeleton anchor;
  * ``contract_surface(ir, node_id)`` — {name: args-or-None} of the node's
    contracted module surface (the engine's registration-refresh input).

Test: tests/audit/test_skeleton_compiler.py,
tests/audit/test_skeleton_write_door.py. Stdlib-only by charter.
"""
from __future__ import annotations

import ast
import sys
from typing import Any, Optional

# the platform dispatcher ABI every route handler is called with
_HANDLER_ARGS = ("payload", "query")
# greppable anchor prefix; conformance checks per-symbol presence
_ANCHOR = "AICODE-NOTE: skeleton-contract"
# the engine skeleton presents itself with this docstring marker; anchor
# integrity is enforced only for deliveries that carry engine text
_MARKER = "Engine-compiled skeleton for node"

# minimal fallback for interpreters without sys.stdlib_module_names —
# structural safety only, 3.10+ always provides the full set
_STDLIB_FALLBACK = frozenset((
    "__future__", "abc", "argparse", "ast", "base64", "collections",
    "contextlib", "csv", "dataclasses", "datetime", "decimal", "enum",
    "functools", "hashlib", "html", "http", "io", "itertools", "json",
    "logging", "math", "os", "pathlib", "random", "re", "sqlite3",
    "string", "sys", "tempfile", "textwrap", "threading", "time",
    "types", "typing", "unittest", "urllib", "uuid", "wsgiref"))
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", _STDLIB_FALLBACK))


def _node_entry(ir: Any, node_id: str) -> dict:
    """Why: one lookup with a total, no-guess contract (absent -> {}).
    What: returns the node's IR entry mapping or an empty dict.
    Test: compile_skeleton('ghost') == '' in test_skeleton_compiler."""
    if not isinstance(ir, dict):
        return {}
    node = (ir.get("nodes") or {}).get(str(node_id))
    return node if isinstance(node, dict) else {}


def _req_fields(op: dict) -> list:
    """Why: the contracted request-body fields must be visible at the def.
    What: returns the requestBody JSON-schema `required` field names.
    Test: 'text' appears in the post_notes anchor (compiler tests)."""
    schema = ((((op.get("requestBody") or {}).get("content") or {})
               .get("application/json") or {}).get("schema") or {})
    return [str(f) for f in (schema.get("required") or [])]


def _op_contract(method: str, path: str, op: dict) -> str:
    """Why: the anchor names the EXACT contract so a rework sees it inline.
    What: one-line 'METHOD /path -> status [media]; body fields: ...' text.
    Test: anchor content asserts in test_skeleton_anchors_name_the_contract.
    """
    detail = "%s %s" % (method.upper(), path)
    responses = op.get("responses") or {}
    for status in sorted(responses):
        detail += " -> %s" % status
        content = (responses.get(status) or {}).get("content") or {}
        medias = sorted(content)
        if medias:
            detail += " [%s]" % ", ".join(medias)
        break                        # the IR contracts ONE success response
    fields = _req_fields(op)
    if fields:
        detail += "; body fields: %s" % ", ".join(fields)
    return detail


def _surface_entries(ir: Any, node_id: str) -> list:
    """Why: ONE derivation both the compiler and the conformance check read
    — the two can never disagree on what the node's surface is (P1).
    What: ordered entries {name, args (list|None), detail} — openapi
    handlers first (platform ABI args), then declared exposes not already
    present. Empty list when the node has no interface.
    Test: determinism + branch/ghost emptiness in test_skeleton_compiler."""
    node = _node_entry(ir, node_id)
    if not node or node.get("children"):
        return []
    entries: list = []
    seen: set = set()
    paths = ((node.get("openapi") or {}).get("paths") or {})
    for path in sorted(paths):
        ops = paths.get(path) or {}
        for method in sorted(ops):
            op = ops.get(method) or {}
            name = str(op.get("x-spec-flow-handler") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append({"name": name, "args": list(_HANDLER_ARGS),
                            "detail": _op_contract(method, path, op)})
    for ent in ((node.get("symbols") or {}).get("exposes") or []):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        args = ent.get("args")
        args = [str(a) for a in args] if isinstance(args, list) else None
        shown = "%s(%s)" % (name, ", ".join(args)) if args is not None \
            else name
        entries.append({"name": name, "args": args,
                        "detail": "exposed symbol %s" % shown})
    return entries


def _allowed_modules(ir: Any, node_id: str) -> set:
    """Why: the import surface is CLOSED — only declared dependencies.
    What: the owner-module stems from symbols.consumes.
    Test: phantom-import findings in test_skeleton_compiler."""
    node = _node_entry(ir, node_id)
    out: set = set()
    for ent in ((node.get("symbols") or {}).get("consumes") or []):
        if isinstance(ent, dict) and ent.get("from"):
            out.add(str(ent["from"]))
    # H5/S13.7: a declared third-party dependency is an admitted import — the
    # spec REQUESTED it (product.requirements + node.dependencies). The wall
    # still stands for anything undeclared.
    for dep in (node.get("dependencies") or []):
        out.add(str(dep))
    return out


def contract_surface(ir: Any, node_id: str) -> dict:
    """Why: the engine's registration refresh must ask 'does this node's
    skeleton still cover the module surface?' without recompiling text.
    What: {contracted name: args list or None (arity uncontracted)}.
    Test: exercised via the route-growth drop in test_skeleton_write_door.
    """
    return {e["name"]: e["args"] for e in _surface_entries(ir, node_id)}


def compile_skeleton(ir: Any, node_id: str) -> str:
    """Why: an engine-written skeleton leaves the model ZERO freedom over
    signatures, imports and route surface — the drift classes die by
    construction, not by post-hoc audit.
    What: deterministic module text — docstring, allowed imports from
    symbols.consumes, env access points, one def per contracted symbol
    with an anchor and a single `raise NotImplementedError` placeholder.
    Empty string when the node has no IR interface (fallback path).
    Test: tests/audit/test_skeleton_compiler.py (S17.1)."""
    entries = _surface_entries(ir, node_id)
    if not entries:
        return ""
    node = _node_entry(ir, node_id)
    nid = str(node_id)
    lines = [
        '"""Engine-compiled skeleton for node %s (spec-flow IR).' % nid,
        "",
        "ENGINE-OWNED SURFACE: the signatures, imports and anchors below",
        "ARE the contract. Fill ONLY the function bodies (replace each",
        "`raise NotImplementedError`). The write door refuses a delivery",
        "that renames a function, changes an argument list, adds a public",
        "function/route beyond this file, or imports anything beyond the",
        'list below plus the standard library."""',
    ]
    # allowed imports line-up: consumed symbols grouped by owner module
    by_owner: dict = {}
    for ent in ((node.get("symbols") or {}).get("consumes") or []):
        if isinstance(ent, dict) and ent.get("from") and ent.get("name"):
            by_owner.setdefault(str(ent["from"]), set()).add(
                str(ent["name"]))
    for owner in sorted(by_owner):
        lines.append("from %s import %s"
                     % (owner, ", ".join(sorted(by_owner[owner]))))
    # env access points: declared vars with their value rules, read via
    # os.environ INSIDE bodies (never cached at import — fresh per call)
    env = [e for e in (node.get("env") or [])
           if isinstance(e, dict) and e.get("name")]
    if env:
        lines.append("")
        lines.append("# AICODE-NOTE: declared env access points -- read via"
                     " os.environ at call time:")
        for ent in env:
            lines.append("#   %s -- rule: %s"
                         % (ent["name"], ent.get("rule") or "(none)"))
    for ent in entries:
        lines.append("")
        lines.append("")
        if ent["args"] is None:
            # arity uncontracted: the symbol must exist at module level,
            # its shape is the implementation's own business
            lines.append("# %s %s %s -- bind this symbol at module level"
                         % (_ANCHOR, ent["name"], ent["detail"]))
            continue
        lines.append("def %s(%s):" % (ent["name"], ", ".join(ent["args"])))
        lines.append("    # %s %s %s" % (_ANCHOR, ent["name"],
                                         ent["detail"]))
        lines.append("    raise NotImplementedError")
    lines.append("")
    return "\n".join(lines)


def _module_surface(tree: ast.Module) -> tuple:
    """Why: conformance judges the PUBLIC module surface, one AST pass.
    What: (defs {name: positional arg names}, bound module-level names).
    Test: private-helper green case in test_skeleton_compiler."""
    defs: dict = {}
    bound: set = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[st.name] = [a.arg for a in
                             list(st.args.posonlyargs) + list(st.args.args)]
            bound.add(st.name)
        elif isinstance(st, ast.ClassDef):
            defs[st.name] = None            # a class is public surface too
            bound.add(st.name)
        elif isinstance(st, ast.Assign):
            bound.update(t.id for t in st.targets
                         if isinstance(t, ast.Name))
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target,
                                                          ast.Name):
            bound.add(st.target.id)
    return defs, bound


# H6/S17.5: effectful surfaces inside a delivered body. Import of these
# modules IS the capability (flagged even if unused, like a loaded weapon).
_IMPORT_EFFECT = {
    "subprocess": "subprocess",
    "socket": "network", "ssl": "network", "ftplib": "network",
    "smtplib": "network", "socketserver": "network",
    "http.client": "network", "urllib.request": "network",
    "shutil": "fs-write", "tempfile": "fs-write",
}
# os.<name> calls: process control and filesystem/env mutation.
_OS_PROC = {"system", "popen", "fork", "kill", "killpg", "execv", "execve",
            "execl", "execle", "execlp", "execvp", "execvpe", "spawnl",
            "spawnv", "spawnve", "spawnvp"}
_OS_FS = {"remove", "unlink", "rename", "replace", "mkdir", "makedirs",
          "rmdir", "removedirs", "chmod", "chown", "symlink", "link",
          "truncate", "mknod"}
_OS_ENV = {"putenv", "unsetenv"}
# pathlib write methods (names chosen to NOT collide with str/other methods:
# no `replace`/`rename` here — str.replace must stay green, v151 lesson).
_PATH_WRITE = {"write_text", "write_bytes", "mkdir", "unlink", "touch",
               "rmdir"}
_ENV_MUT_METHODS = {"pop", "update", "setdefault", "clear", "popitem"}


def _node_effects(ir: Any, node_id: str) -> set:
    """Effect classes the node is CONTRACTED to perform (optional `effects`
    datum). Absent => empty set => deny all (the safe closed-world default)."""
    nodes = (ir or {}).get("nodes") if isinstance(ir, dict) else None
    node = (nodes or {}).get(str(node_id)) if isinstance(nodes, dict) else None
    eff = node.get("effects") if isinstance(node, dict) else None
    return {str(e) for e in eff} if isinstance(eff, (list, tuple, set)) else set()


def _import_effect(dotted: str) -> Optional[str]:
    if dotted in _IMPORT_EFFECT:
        return _IMPORT_EFFECT[dotted]
    return None


def _open_mode_is_write(call: "ast.Call") -> bool:
    """open(path[, mode]) — a write iff the mode is a write/append/create/
    update literal OR a mode the AST cannot prove read-only (closed world)."""
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    else:
        for kw in call.keywords:
            if kw.arg == "mode":
                mode = kw.value
    if mode is None:
        return False                        # open(path) — read
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "waxate+") \
            and set(mode.value) - set("rbtU") != set()
    return True                              # unprovable mode -> deny


def body_effect_findings(tree: "ast.AST", node_id: str, allowed: set) -> list:
    """H6/S17.5: the function BODY lives in a closed EFFECT world.

    Why: the skeleton door closed the import world and the public surface,
    but a delivered body could still spawn processes, open sockets, mutate
    the process environment or write arbitrary files (finding F2, proven:
    such a body passed with ZERO findings). Nothing is invented at the
    product surface — but observable SIDE EFFECTS the spec never asked for
    are exactly the model inventing behaviour.
    What: walks the body AST; an effectful stdlib surface is a finding unless
    its class is in the node's declared `effects`. Reads are green (a read
    mutates nothing observable and leaves may read their own data files);
    writes, process control, network, env mutation and dynamic import are
    findings. Classes: subprocess, network, env-write, fs-write,
    dynamic-import.
    Test: tests/audit/test_body_effect_door.py."""
    nid = str(node_id)
    out: list = []

    def flag(cls, detail):
        if cls in allowed:
            return
        out.append("node %s: body performs %s (%s) — not in the node's "
                   "declared effects (closed effect world)" % (nid, cls, detail))

    for st in ast.walk(tree):
        # capability imports
        if isinstance(st, ast.Import):
            for a in st.names:
                cls = _import_effect(a.name)
                if cls:
                    flag(cls, "import %s" % a.name)
        elif isinstance(st, ast.ImportFrom) and not st.level:
            mod = st.module or ""
            cls = _import_effect(mod)
            if cls is None:
                for a in st.names:
                    cls = _import_effect("%s.%s" % (mod, a.name))
                    if cls:
                        break
            if cls:
                flag(cls, "from %s import ..." % mod)
        # calls
        elif isinstance(st, ast.Call):
            f = st.func
            # open(..., write-mode)
            if isinstance(f, ast.Name) and f.id == "open" \
                    and _open_mode_is_write(st):
                flag("fs-write", "open() in a write mode")
            elif isinstance(f, ast.Name) and f.id == "__import__":
                flag("dynamic-import", "__import__()")
            elif isinstance(f, ast.Attribute):
                attr = f.attr
                base = f.value.id if isinstance(f.value, ast.Name) else None
                if base == "os":
                    if attr in _OS_PROC:
                        flag("subprocess", "os.%s" % attr)
                    elif attr in _OS_FS:
                        flag("fs-write", "os.%s" % attr)
                    elif attr in _OS_ENV:
                        flag("env-write", "os.%s" % attr)
                elif attr in _PATH_WRITE:
                    flag("fs-write", "pathlib .%s()" % attr)
                elif attr == "import_module" and base == "importlib":
                    flag("dynamic-import", "importlib.import_module()")
                # os.environ.<mutator>(...)
                elif attr in _ENV_MUT_METHODS \
                        and isinstance(f.value, ast.Attribute) \
                        and f.value.attr == "environ":
                    flag("env-write", "os.environ.%s" % attr)
        # os.environ[...] = / del os.environ[...]
        elif isinstance(st, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = st.targets if isinstance(st, ast.Assign) \
                else ([st.target] if isinstance(st, ast.AugAssign)
                      else st.targets)
            for tgt in targets:
                if isinstance(tgt, ast.Subscript) \
                        and isinstance(tgt.value, ast.Attribute) \
                        and tgt.value.attr == "environ":
                    flag("env-write", "os.environ[] mutation")
    return out


def skeleton_conformance(ir: Any, node_id: str, code: str) -> list:
    """Why: the skeleton is only a contract if the door can HOLD it — every
    finding is attributable (node id + offending symbol, P4).
    What: findings for (a) missing/rewritten contracted signatures, (b)
    imports outside symbols.consumes + stdlib (the v157 class), (c) public
    defs/classes beyond the IR (closed world), (d) anchors stripped from a
    delivery that presents engine skeleton text (a fresh conforming module
    without engine text passes on the data checks alone).
    Empty when the node has no IR interface (inert fallback) or the code
    does not parse (the suite owns a SyntaxError, S10.12 convention).
    Test: tests/audit/test_skeleton_compiler.py (S17.2),
    tests/audit/test_skeleton_write_door.py (S17.3)."""
    entries = _surface_entries(ir, node_id)
    if not entries:
        return []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    nid = str(node_id)
    findings: list = []
    defs, bound = _module_surface(tree)
    # (a) signatures are engine-owned
    for ent in entries:
        name, want = ent["name"], ent["args"]
        if want is None:
            if name not in bound:
                findings.append(
                    "node %s: contracted symbol '%s' is not bound at module"
                    " level" % (nid, name))
            continue
        got = defs.get(name)
        if name not in defs:
            findings.append(
                "node %s: contracted function '%s(%s)' is missing — the"
                " skeleton signature is engine-owned"
                % (nid, name, ", ".join(want)))
        elif got != want:
            findings.append(
                "node %s: signature of '%s' rewritten — skeleton orders"
                " (%s), delivery has (%s)"
                % (nid, name, ", ".join(want),
                   ", ".join(got) if got is not None else "a class"))
    # (b) closed import world: consumed owners + stdlib, nothing else
    allowed = _allowed_modules(ir, node_id)
    seen_imports: set = set()
    for st in ast.walk(tree):
        tops: list = []
        if isinstance(st, ast.Import):
            tops = [a.name.split(".")[0] for a in st.names]
        elif isinstance(st, ast.ImportFrom):
            if st.level:
                tops = []                   # flat product: no packages
                findings.append(
                    "node %s: relative import is outside the skeleton"
                    " contract (the product tree is flat)" % nid)
            else:
                tops = [(st.module or "").split(".")[0]]
        for top in tops:
            if (not top or top in allowed or top in _STDLIB
                    or top in seen_imports):
                continue
            seen_imports.add(top)
            findings.append(
                "node %s: import '%s' is outside the allowed list"
                " (consumes: %s; stdlib is allowed)"
                % (nid, top, ", ".join(sorted(allowed)) or "nothing"))
    # (c) closed public surface: no uncontracted routes/functions
    contracted = {e["name"] for e in entries}
    for name in sorted(defs):
        if not name.startswith("_") and name not in contracted:
            findings.append(
                "node %s: public %s '%s' is not in the IR interface"
                " (closed world)"
                % (nid, "class" if defs[name] is None else "function",
                   name))
    # (d) anchors are ENGINE-TEXT integrity, not a comment tax: a delivery
    # that PRESENTS engine skeleton text (the docstring marker or any
    # anchor) with anchors stripped/tampered is a skeleton edit; a fresh
    # conforming module without engine text is judged on the DATA checks
    # above alone (v151 lesson: a gate false-positive on a legitimate
    # delivery sinks a run)
    text = code or ""
    if _MARKER in text or _ANCHOR in text:
        for ent in entries:
            if "%s %s" % (_ANCHOR, ent["name"]) not in text:
                findings.append(
                    "node %s: skeleton anchor for '%s' stripped — anchors"
                    " are part of the contract" % (nid, ent["name"]))
    # (e) H6/S17.5: the body lives in a closed effect world — an observable
    # side effect the node never declared is behaviour the spec never asked
    # for (finding F2). Shares the checker with the doctor's repair door.
    findings += body_effect_findings(tree, nid, _node_effects(ir, node_id))
    return findings
