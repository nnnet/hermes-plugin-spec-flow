"""External contract-oracle harnesses (node B2, S16.5).

Why: Schemathesis (pip) and Specmatic (java jar) validate the compiled
OpenAPI document against a LIVE server; their availability is an
environment fact, so each harness carries an honest `probe()` alongside a
unit-testable `build_command()`.
What: run_schemathesis / run_specmatic — command assembly + availability
probes + a thin CLI wrapper each.
Test: tests/audit/test_openapi_compiler.py (S16.5) — inspection level, no
network, no external binaries required.
"""
