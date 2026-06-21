"""442: a node that answers a human requirement carries that requirement into
its spec as an explicit coverage criterion, so the spec — and the reviewer who
reads it — can derive the acceptance check FROM the requirement.

Model-independent: the engine surfaces whatever statement it attached to the
node; it invents no project-specific wording (no route/app/web literals)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Workspace  # noqa: E402


def _spec_text(node: dict) -> str:
    with tempfile.TemporaryDirectory() as d:
        ws = Workspace(root=d, enabled=True).open()
        rel = ws.spec(node_id=node["id"], title=node.get("title", node["id"]),
                      depth=1, verdict="leaf", reasons="", parent="L0:root",
                      plan_lines=["build it"], node=node, module=node["id"])
        return (ws_path := os.path.join(d, rel)) and open(ws_path,
                                                           encoding="utf-8").read()


def test_requirement_node_surfaces_coverage_criterion():
    node = {"id": "ui_view", "title": "Browser view",
            "requirement": "A человек открывает страницу в браузере и видит список."}
    txt = _spec_text(node)
    assert "Covers human requirement:" in txt
    # the EXACT human statement rides in — not an engine-invented paraphrase
    assert "видит список" in txt
    assert "Acceptance (coverage criterion):" in txt
    assert "RED until" in txt


def test_plain_node_has_no_coverage_line():
    txt = _spec_text({"id": "plain", "title": "Plain leaf"})
    assert "Covers human requirement:" not in txt


def test_no_hardcoded_product_wording():
    # the engine must not inject web/app/route literals of its own — the
    # criterion text is generic and only echoes the human statement.
    node = {"id": "lib_fn", "title": "Pure helper",
            "requirement": "Library exposes a parse() callable returning a dict."}
    txt = _spec_text(node)
    assert "parse()" in txt
    for leak in ("/ui", "wsgi", "src/app.py", "/health"):
        assert leak not in txt
