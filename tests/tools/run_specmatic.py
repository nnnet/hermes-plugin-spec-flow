"""Specmatic harness — contract-test a live server against openapi.json.

Why: Specmatic is the second, independent contract oracle (java jar) — two
oracles disagreeing is itself a signal.  Java and the jar are environment
facts, so command assembly is unit-tested separately from execution.
What: build_command() assembles the `java -jar` invocation, probe() reports
what is present (java on PATH / jar on disk), main() runs the oracle or
says exactly what a human must provide.
Test: tests/audit/test_openapi_compiler.py::test_specmatic_harness_command_assembly
and ::test_probes_report_availability_honestly (no execution needed).

Usage:
    python3 tests/tools/run_specmatic.py <openapi.json> <base-url> [jar-path]
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

# Default drop location for the jar, relative to this file — a human
# downloads it from https://specmatic.io (no pip channel exists).
_DEFAULT_JAR = pathlib.Path(__file__).resolve().parent / "specmatic.jar"


def build_command(jar_path: str, schema_path: str, base_url: str) -> list:
    """Why: the invocation shape must be correct even where java is absent.
    What: `java -jar <jar> test <spec> --testBaseURL=<base>` — Specmatic's
    documented contract-test invocation.
    Test: assembled list is asserted element-by-element in S16.5.
    """
    return ["java", "-jar", str(jar_path), "test", str(schema_path),
            "--testBaseURL=%s" % base_url]


def probe(jar_path: str | None = None) -> dict:
    """Why: the [!] human-needed list must name WHAT is missing — a JRE, the
    jar, or both.
    What: returns {"available": bool, "java": path|None, "jar": path|None};
    available only when both java and the jar exist.
    Test: test_probes_report_availability_honestly (keys + bool type).
    """
    java = shutil.which("java")
    candidate = pathlib.Path(jar_path) if jar_path else _DEFAULT_JAR
    jar = str(candidate) if candidate.is_file() else None
    return {"available": bool(java and jar), "java": java, "jar": jar}


def main(argv: list) -> int:
    """Why: one runnable door for the oracle so runs are reproducible.
    What: probes, then runs the assembled command; exits 2 with a precise
    human-needed message when java or the jar is missing.
    Test: assembly and probe are the unit-tested parts; execution needs the
    environment a human provides.
    """
    if len(argv) < 2:
        print("usage: run_specmatic.py <openapi.json> <base-url> "
              "[jar-path]", file=sys.stderr)
        return 2
    jar_arg = argv[2] if len(argv) > 2 else None
    status = probe(jar_arg)
    if not status["available"]:
        print("specmatic is not available: java=%r jar=%r\n"
              "human-needed: install a JRE (e.g. apt install "
              "default-jre-headless) and download specmatic.jar from "
              "https://specmatic.io to %s" % (status["java"],
                                              status["jar"], _DEFAULT_JAR),
              file=sys.stderr)
        return 2
    cmd = build_command(status["jar"], argv[0], argv[1])
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
