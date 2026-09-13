#!/usr/bin/env python3
"""Small stdio MCP exposing bounded, repository-aware code navigation."""

import json
import sys

import code_nav

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "repository_route", "description": "Select relevant paths from the repository map. Call this before searching.",
     "inputSchema": {"type": "object", "required": ["root", "query"], "properties": {
         "root": {"type": "string"}, "query": {"type": "string"}}}},
    {"name": "search_literal", "description": "Run one exact literal ripgrep in one repository, preferably over routed paths.",
     "inputSchema": {"type": "object", "required": ["root", "text"], "properties": {
         "root": {"type": "string"}, "text": {"type": "string"},
         "route_paths": {"type": "array", "items": {"type": "string"}},
         "max_results": {"type": "integer", "minimum": 1, "default": 50},
         "exhaustive": {"type": "boolean", "default": False}}}},
    {"name": "symbol_index", "description": "List symbols with ctags or Python AST for explicit routed paths only.",
     "inputSchema": {"type": "object", "required": ["root", "paths"], "properties": {
         "root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "code_nav_doctor", "description": "Check repository map, host/container navigation dependencies and indexes.",
     "inputSchema": {"type": "object", "required": ["root"], "properties": {"root": {"type": "string"}}}},
]


def invoke(name, args):
    root = code_nav.discover_repository(args["root"])
    if name == "repository_route":
        return code_nav.route_repository(root, args["query"])
    if name == "search_literal":
        return code_nav.search_literal(root, args["text"], args.get("route_paths"),
                                       args.get("max_results", 50), args.get("exhaustive", False))
    if name == "symbol_index":
        return code_nav.symbol_index(root, args["paths"])
    if name == "code_nav_doctor":
        return code_nav.doctor(root)
    raise ValueError("unknown tool: %s" % name)


def reply(req, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": req.get("id")}
    if error:
        payload["error"] = {"code": -32000, "message": str(error)}
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
            method = req.get("method")
            if method == "initialize":
                reply(req, {"protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "code-nav", "version": "0.1.0"}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                reply(req, {"tools": TOOLS})
            elif method == "tools/call":
                params = req.get("params", {})
                value = invoke(params.get("name"), params.get("arguments", {}))
                reply(req, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
            else:
                reply(req, error="unsupported method: %s" % method)
        except Exception as exc:
            reply(locals().get("req", {}), error=exc)


if __name__ == "__main__":
    main()
