"""Schemathesis harness — property-fuzz a live server against openapi.json.

Why: the compiled document (spec_openapi.compile_openapi) is only worth
anything if an EXTERNAL oracle can consume it unmodified; Schemathesis is
the pip-installable one.  Availability is an environment fact, so the
command assembly is unit-tested separately from execution.
What: build_command() assembles the CLI invocation, probe() reports what is
present (CLI on PATH / importable package), main() runs the oracle or says
exactly what a human must provide.
Test: tests/audit/test_openapi_compiler.py::test_schemathesis_harness_command_assembly
and ::test_probes_report_availability_honestly (no execution needed).

Usage:
    python3 tests/tools/run_schemathesis.py <openapi.json> <base-url> [extra..]
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys


def build_command(schema_path: str, base_url: str,
                  extra_args: list | None = None) -> list:
    """Why: the CLI shape must be correct even on hosts where schemathesis
    is not installed — assembly is testable, execution is environmental.
    What: `schemathesis run <schema> --url <base>` (v4 CLI) plus verbatim
    pass-through extras.
    Test: assembled list is asserted element-by-element in S16.5.
    """
    cmd = ["schemathesis", "run", str(schema_path), "--url", str(base_url)]
    if extra_args:
        cmd.extend(str(a) for a in extra_args)
    return cmd


def probe() -> dict:
    """Why: the [!] human-needed list must name WHAT is missing, not just
    say "unavailable".
    What: returns {"available": bool, "cli": path|None, "package": bool} —
    available when either the CLI is on PATH or the package imports (then
    `python -m schemathesis` works).
    Test: test_probes_report_availability_honestly (keys + bool type).
    """
    cli = shutil.which("schemathesis")
    package = importlib.util.find_spec("schemathesis") is not None
    return {"available": bool(cli) or package, "cli": cli,
            "package": package}


def main(argv: list) -> int:
    """Why: one runnable door for the oracle so runs are reproducible.
    What: probes, then execs the assembled command (falling back to
    `python -m schemathesis` when only the package is present); exits 2
    with a precise human-needed message when nothing is available.
    Test: exercised manually (demo run in the B2 report); assembly and
    probe are the unit-tested parts.
    """
    if len(argv) < 2:
        print("usage: run_schemathesis.py <openapi.json> <base-url> "
              "[extra args...]", file=sys.stderr)
        return 2
    status = probe()
    if not status["available"]:
        print("schemathesis is not available: cli=%r package=%r\n"
              "human-needed: pip install schemathesis (network + writable "
              "site-packages required)" % (status["cli"],
                                           status["package"]),
              file=sys.stderr)
        return 2
    cmd = build_command(argv[0], argv[1], argv[2:])
    if not status["cli"]:
        cmd = [sys.executable, "-m", "schemathesis"] + cmd[1:]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
