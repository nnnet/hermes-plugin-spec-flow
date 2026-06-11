"""Battle test — OpenAPI contract → MCP tool manifest (roadmap B5).

A node that consumes an API gets the API as MCP tools generated from the frozen
OpenAPI contract: one tool per operation, with a JSON-Schema input built from
its path/query parameters and request body. Self-contained generator, verified
WITHOUT Hermes or an external openapi-mcp lib.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Orders API"},
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "summary": "List orders",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer"}},
                ],
            },
            "post": {
                "operationId": "createOrder",
                "summary": "Create an order",
                "requestBody": {"content": {"application/json": {
                    "schema": {"type": "object"}}}},
            },
        },
        "/orders/{id}": {
            "get": {
                "operationId": "getOrder",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                ],
            },
        },
    },
}


def test_one_tool_per_operation(plugin):
    tools = plugin.tools.openapi_to_mcp_tools(SPEC)
    names = {t["name"] for t in tools}
    assert names == {"listOrders", "createOrder", "getOrder"}


def test_path_param_is_required_input(plugin):
    tools = {t["name"]: t for t in plugin.tools.openapi_to_mcp_tools(SPEC)}
    get_one = tools["getOrder"]
    assert "id" in get_one["input_schema"]["properties"]
    assert "id" in get_one["input_schema"]["required"]
    assert get_one["method"] == "GET" and get_one["path"] == "/orders/{id}"


def test_request_body_becomes_input(plugin):
    tools = {t["name"]: t for t in plugin.tools.openapi_to_mcp_tools(SPEC)}
    create = tools["createOrder"]
    assert "body" in create["input_schema"]["properties"]
    # OpenAPI default: a request body is optional unless required: true
    assert "body" not in create["input_schema"]["required"]


def test_required_request_body_is_required_input(plugin):
    spec = {"info": {"title": "X"}, "paths": {"/x": {"post": {
        "operationId": "makeX",
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"type": "object"}}}},
    }}}}
    tools = {t["name"]: t for t in plugin.tools.openapi_to_mcp_tools(spec)}
    assert "body" in tools["makeX"]["input_schema"]["required"]


def test_optional_query_not_required(plugin):
    tools = {t["name"]: t for t in plugin.tools.openapi_to_mcp_tools(SPEC)}
    lst = tools["listOrders"]
    assert "limit" in lst["input_schema"]["properties"]
    assert "limit" not in lst["input_schema"]["required"]


def test_tool_handler_with_spec_object(plugin):
    out = json.loads(plugin.tools._handle_openapi_mcp({"spec": SPEC}))
    assert out["tool_count"] == 3
    assert out["server"] == "orders_api_mcp"


def test_tool_handler_reads_yaml_file(plugin, tmp_path):
    import yaml
    p = tmp_path / "api.yaml"
    p.write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    out = json.loads(plugin.tools._handle_openapi_mcp({"contract": str(p)}))
    assert out["tool_count"] == 3


def test_rejects_non_openapi(plugin):
    out = json.loads(plugin.tools._handle_openapi_mcp({"spec": {"not": "openapi"}}))
    assert "error" in out


def test_tool_registered(plugin):
    assert "openapi_mcp" in plugin.reg.tools
