"""Thin re-export of the PLUGIN's smart verification oracle.

The oracle now lives in the plugin (`spec_flow_tools`), so the reports it
produces are built by plugin code — a function of the plugin, exactly like
`build_run_report`. This shim only re-exports the names tests/report code used
(`check`, `render`, `OracleReport`, `Expectation`) and ensures the plugin
module is loaded standalone (no Hermes needed).

See `spec_flow_tools.check_oracle` / `render_oracle` / `build_oracle_report`.
"""

from __future__ import annotations

from harness import run_engine as _eng  # noqa: F401 — ensures spec_flow_tools is loaded
import spec_flow_tools as _t

OracleReport = _t.OracleReport
Expectation = _t.Expectation
check = _t.check_oracle
render = _t.render_oracle
build_oracle_report = _t.build_oracle_report
