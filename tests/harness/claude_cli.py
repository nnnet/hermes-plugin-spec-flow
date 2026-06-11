"""Shared bits for invoking the local ``claude`` CLI from the test harness."""

from __future__ import annotations

import json
import os
from pathlib import Path


def claude_cmd() -> list[str]:
    """Base argv to launch the claude CLI, optionally routed through
    ``headroom wrap claude`` for visibility / context optimization.

    Off by default (plain ``claude``) so offline tests stay fast and need no
    extra binary. Enable via env ``SPEC_FLOW_HEADROOM_WRAP``:
      * ``1`` / ``on`` / ``true``  → use ``headroom`` from PATH
      * any other value           → treated as the headroom binary path
        (e.g. ``/path/to/.venv/bin/headroom`` — kept out of the code, in env)

    Wrapped form is ``headroom wrap claude -- <claude args>``; the caller appends
    its claude flags after this prefix.
    """
    hr = os.environ.get("SPEC_FLOW_HEADROOM_WRAP", "").strip()
    if hr and hr.lower() not in ("0", "false", "no", "off"):
        binary = "headroom" if hr.lower() in ("1", "true", "yes", "on") else hr
        return [binary, "wrap", "claude", "--"]
    return ["claude"]


def strip_headroom_banner(text: str) -> str:
    """Drop the ``headroom wrap`` banner (box + proxy/launch/telemetry status)
    so only the model's response remains. Banner lines are blank or indented; the
    model's output starts at the first column-0 non-blank line. No-op when the
    output was not produced through headroom (plain claude starts at column 0)."""
    if "HEADROOM WRAP" not in text:
        return text
    out, started = [], False
    for ln in text.splitlines():
        if not started:
            if ln.strip() == "" or ln.startswith("  "):
                continue
            started = True
        out.append(ln)
    return "\n".join(out)


def mcp_args_no_serena() -> list[str]:
    """CLI args reproducing the project's MCP servers minus serena.

    One-shot ``claude -p`` jobs never call serena tools, yet each spawn drags
    in uvx + python + an LSP server (~10s startup, ~200 MB RSS, and an extra
    dashboard port).  ``--strict-mcp-config`` makes claude use ONLY the config
    given here, so we merge the global (~/.claude.json) and nearest project
    (.mcp.json) server maps ourselves and drop just serena — every other
    server stays available to the job.
    """
    servers: dict = {}
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text())
        servers.update(cfg.get("mcpServers", {}))
    except (OSError, json.JSONDecodeError):
        pass
    for d in Path(__file__).resolve().parents:
        f = d / ".mcp.json"
        if f.is_file():
            try:
                servers.update(json.loads(f.read_text()).get("mcpServers", {}))
            except (OSError, json.JSONDecodeError):
                pass
            break
    servers.pop("serena", None)
    return ["--strict-mcp-config", "--mcp-config",
            json.dumps({"mcpServers": servers})]
