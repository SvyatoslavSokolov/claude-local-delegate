#!/usr/bin/env python3
"""One-tool MCP for repository-map routing; Serena owns code intelligence."""
import json
import sys

import code_nav

TOOL = {
    "name": "repository_route",
    "description": "Select child maps and paths, then use Serena for code intelligence.",
    "inputSchema": {"type": "object", "required": ["root", "query"],
                    "properties": {"root": {"type": "string"}, "query": {"type": "string"}}},
}


def reply(request, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request.get("id")}
    payload["error" if error else "result"] = (
        {"code": -32000, "message": str(error)} if error else result)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                reply(request, {"protocolVersion": "2024-11-05",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "repository-router",
                                               "version": "1.0.0"}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                reply(request, {"tools": [TOOL]})
            elif method == "tools/call":
                params = request.get("params", {})
                if params.get("name") != TOOL["name"]:
                    raise ValueError("unknown tool")
                args = params.get("arguments", {})
                value = code_nav.route_repository(args["root"], args["query"])
                reply(request, {"content": [{"type": "text",
                                             "text": json.dumps(value, ensure_ascii=False)}]})
            else:
                reply(request, error="unsupported method")
        except Exception as exc:
            reply(request, error=exc)


if __name__ == "__main__":
    main()
