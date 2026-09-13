"""Thin repository-map router; Serena owns semantic code navigation."""
from __future__ import annotations

import json
import os
import re
import subprocess

MAP_CANDIDATES = ("docs/design/repository_map.yaml", "repository_map.yaml")
STOPWORDS = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on",
             "is", "are", "be", "with", "use", "find", "where", "what", "how",
             "why", "when", "which", "this", "that", "it", "its", "from",
             "through", "into", "by", "at"}


def _run(command, cwd):
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, check=False)


def discover_repository(start):
    path = os.path.abspath(os.path.expanduser(str(start)))
    try:
        proc = _run(["git", "rev-parse", "--show-toplevel"], path)
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    current = path
    while True:
        if os.path.isdir(os.path.join(current, ".git")) or any(
            os.path.isfile(os.path.join(current, item)) for item in MAP_CANDIDATES
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return path
        current = parent


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _simple_yaml_load(text):
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ValueError("odd YAML indentation")
        lines.append((indent // 2, stripped))
    cursor = [0]

    def value(raw):
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except ValueError:
                return [_scalar(item) for item in raw[1:-1].split(",") if item.strip()]
        return _scalar(raw)

    def block(level):
        result = None
        while cursor[0] < len(lines):
            indent, content = lines[cursor[0]]
            if indent < level:
                break
            if indent > level:
                raise ValueError("unexpected YAML indentation")
            if content.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    raise ValueError("mixed YAML block")
                tail = content[2:]
                if ":" in tail:
                    key, _, rest = tail.partition(":")
                    item = {_scalar(key): value(rest.strip()) if rest.strip() else None}
                    cursor[0] += 1
                    if cursor[0] < len(lines) and lines[cursor[0]][0] > level:
                        extra = block(lines[cursor[0]][0])
                        if isinstance(extra, dict):
                            item.update(extra)
                    result.append(item)
                else:
                    result.append(value(tail))
                    cursor[0] += 1
                continue
            if ":" not in content:
                cursor[0] += 1
                continue
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise ValueError("mixed YAML block")
            key, _, rest = content.partition(":")
            cursor[0] += 1
            if rest.strip():
                result[_scalar(key)] = value(rest.strip())
            elif cursor[0] < len(lines) and lines[cursor[0]][0] > level:
                result[_scalar(key)] = block(lines[cursor[0]][0])
            else:
                result[_scalar(key)] = None
        return result

    return block(0) or {}


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except (ImportError, Exception):
        return _simple_yaml_load(text)


def load_repository_map(root):
    for relative in MAP_CANDIDATES:
        path = os.path.join(str(root), relative)
        if os.path.isfile(path):
            data = _load(path)
            routes = data.get("routes", data.get("task_routes", []))
            return {"map_path": path, "routes": routes if isinstance(routes, list) else []}
    return {"map_path": None, "routes": []}


def _tokens(query):
    return [token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+",
                                          str(query).lower()) if token not in STOPWORDS]


def route_repository(root, query):
    """Return relevant declared routes without walking the repository."""
    root = discover_repository(root)
    loaded = load_repository_map(root)
    scored = []
    for route in loaded["routes"]:
        if not isinstance(route, dict):
            continue
        strings = [str(route.get(key, "")).lower() for key in
                   ("id", "name", "path", "description", "summary", "matches")]
        score = sum(2 for token in _tokens(query) if any(token in item for item in strings))
        if score:
            scored.append({"id": route.get("id", route.get("name", "")), "score": score,
                           "child_maps": list(route.get(
                               "child_maps", route.get("children", [])) or [])})
    scored.sort(key=lambda item: (-item["score"], item["id"]))
    # Do not impose an arbitrary route count: genuinely cross-cutting tasks may
    # span CLI, configuration, runtime and publishing. Keep every route close
    # to the best semantic score, so generic one-token matches do not fan out.
    best = scored[0]["score"] if scored else 0
    selected = [route for route in scored if route["score"] >= best - 2]
    child_ids = []
    for route in selected:
        for child in route["child_maps"]:
            if child not in child_ids:
                child_ids.append(child)

    children, route_paths = [], []
    for child_id in child_ids:
        relative = child_id if "/" in child_id else (
            "docs/design/repository_maps/%s.yaml" % child_id)
        detail = {"map": relative, "paths": []}
        absolute = os.path.join(root, relative)
        if os.path.isfile(absolute):
            data = _load(absolute)
            base = data.get("path", "")
            candidates = [
                entry["path"] for entry in (data.get("entry_points", []) or [])
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            ]
            candidates.extend(item for item in (data.get("key_paths", []) or [])
                              if isinstance(item, str))
            for candidate in candidates:
                path = (candidate if candidate.startswith(base + "/")
                        else os.path.join(base, candidate)).rstrip("/")
                detail["paths"].append(path)
                if path not in route_paths:
                    route_paths.append(path)
        children.append(detail)
    return {"root": root, "map_path": loaded["map_path"], "matched": selected,
            "child_maps": children, "route_paths": route_paths,
            "next": "Activate root in Serena, then use Serena symbol tools within route_paths."}
