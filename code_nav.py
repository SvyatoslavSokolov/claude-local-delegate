"""Code navigation primitives for the local delegation stack.

Stdlib-only. No LSP protocol in core: an external MCP provides prepared
navigation primitives, and Claude LSP is applied separately.

API:
- discover_repository(start)
- load_repository_map(root)
- route_repository(root, query)
- search_literal(root, text, route_paths=None, max_results=50, exhaustive=False)
- symbol_index(root, paths=None)
- doctor(root)
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess

MAP_CANDIDATES = ("docs/design/repository_map.yaml", "repository_map.yaml")

# Standard generated / noisy directories excluded from every FS-touching op.
EXCLUDED_DIRS = {
    ".git", ".cache", "__pycache__", "node_modules", "build", "dist",
    "install", "log", "logs", "target", "out", ".pytest_cache",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "is",
    "are", "be", "with", "use", "using", "used", "find", "where", "what",
    "how", "why", "when", "which", "this", "that", "it", "its",
}


def _run(cmd, cwd):
    """Run a command list as argv (no shell). Returns CompletedProcess."""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=False
    )


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------

def discover_repository(start):
    """Return the repository root for ``start``.

    Primary: ``git rev-parse --show-toplevel`` with cwd=start.
    Fallback: walk up from start looking for a ``.git`` entry or one of
    MAP_CANDIDATES; if nothing is found, return start itself.
    """
    path = os.path.abspath(os.path.expanduser(str(start)))
    try:
        proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        top = proc.stdout.strip()
        if top:
            return top
    current = path
    while True:
        if (os.path.isdir(os.path.join(current, ".git"))
                or any(os.path.exists(os.path.join(current, c))
                       for c in MAP_CANDIDATES)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return path
        current = parent


# ---------------------------------------------------------------------------
# Repository map (bounded YAML, no PyYAML required)
# ---------------------------------------------------------------------------

def _parse_scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _simple_yaml_load(text):
    """Minimal line parser for a restricted subset of YAML.

    Supports nested mappings (2-space indentation), lists of scalars
    ("- item"), inline JSON-compatible lists, quoted strings and comments.
    Raises ValueError on anything it cannot understand so callers can fall
    back to hints instead of guessing structure.
    """
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2 != 0:
            raise ValueError("odd indentation in map file")
        lines.append((indent // 2, stripped))

    pos = [0]

    def parse_value(scalar):
        if scalar.startswith("["):
            try:
                return json.loads(scalar)
            except ValueError:
                inner = scalar.strip("[]")
                return [_parse_scalar(part) for part in inner.split(",") if part.strip()]
        return _parse_scalar(scalar)

    def parse_block(level):
        result = None
        while pos[0] < len(lines):
            indent, content = lines[pos[0]]
            if indent < level:
                break
            if indent > level:
                raise ValueError("unexpected indentation in map file")
            if content.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    raise ValueError("mixed mapping/list at same level")
                result.append(parse_value(content[2:]))
                pos[0] += 1
                continue
            if ":" not in content:
                raise ValueError("unsupported line in map file: %r" % content)
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise ValueError("mixed mapping/list at same level")
            key, _, rest = content.partition(":")
            key = _parse_scalar(key)
            rest = rest.strip()
            pos[0] += 1
            if rest:
                result[key] = parse_value(rest)
            elif pos[0] < len(lines) and lines[pos[0]][0] > level:
                result[key] = parse_block(lines[pos[0]][0])
            else:
                result[key] = None
        return result

    parsed = parse_block(0)
    return parsed if parsed is not None else {}


def load_repository_map(root):
    """Load the repository map without PyYAML.

    Tries PyYAML when importable; otherwise uses the bounded line parser.
    If no map file exists or parsing fails, returns a hint structure with
    ``map_path`` and ``content_hints`` only (no filesystem traversal).
    """
    root = str(root)
    candidates = [os.path.join(root, c) for c in MAP_CANDIDATES]
    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as fh:
                text = fh.read()
            data = None
            try:
                import yaml  # optional dependency
                data = yaml.safe_load(text)
            except ImportError:
                data = None
            except Exception:
                data = None
            if data is None:
                try:
                    data = _simple_yaml_load(text)
                except ValueError:
                    return {
                        "root": root,
                        "map_path": candidate,
                        "parsed": False,
                        "content_hints": _content_hints(text),
                        "routes": [],
                    }
            if not isinstance(data, dict):
                data = {"routes": data} if isinstance(data, list) else {}
            # Older maps used ``routes``; the virtual-hal map deliberately
            # calls the same concept ``task_routes``.
            routes = data.get("routes", data.get("task_routes"))
            if not isinstance(routes, list):
                routes = []
            return {
                "root": root,
                "map_path": candidate,
                "parsed": True,
                "data": data,
                "routes": routes,
            }
    return {
        "root": root,
        "map_path": None,
        "parsed": False,
        "content_hints": [],
        "routes": [],
    }


def _content_hints(text):
    hints = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("- ") or ":" in stripped:
            hints.append(stripped.rstrip(":").strip())
        if len(hints) >= 40:
            break
    return hints


# ---------------------------------------------------------------------------
# Route selection (map only, no filesystem traversal)
# ---------------------------------------------------------------------------

def _tokenize(query):
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", str(query).lower())
    return [t for t in tokens if t not in STOPWORDS]


def _route_strings(route):
    parts = []
    if isinstance(route, dict):
        for key in ("description", "desc", "summary", "name", "title", "path", "id", "matches"):
            value = route.get(key)
            if isinstance(value, str):
                parts.append(value)
        keywords = route.get("keywords")
        if isinstance(keywords, list):
            parts.extend(k for k in keywords if isinstance(k, str))
        children = route.get("children")
        if isinstance(children, list):
            parts.extend(c for c in children if isinstance(c, str))
    elif isinstance(route, str):
        parts.append(route)
    return parts


def route_repository(root, query):
    """Score map routes against a query using only the root map.

    Returns ``{"matched": [...], "child_maps": [...]}`` where matched entries
    are ``{"path": ..., "score": ...}`` sorted by descending score. No
    filesystem traversal beyond reading the single map file.
    """
    loaded = load_repository_map(root)
    tokens = _tokenize(query)
    scored = []
    for route in loaded["routes"]:
        strings = _route_strings(route)
        lowered = [s.lower() for s in strings]
        score = 0
        for token in tokens:
            for s in lowered:
                if token in s:
                    score += 2
                    break
        if isinstance(route, dict):
            path = route.get("path") or route.get("file") or ""
            children = route.get("children", route.get("child_maps"))
            if not isinstance(children, list):
                children = []
            child = route.get("child_map")
            if isinstance(child, str):
                children.append(child)
        else:
            path = str(route)
            children = []
        if score > 0:
            scored.append({"id": route.get("id", "") if isinstance(route, dict) else "",
                           "path": path, "score": score,
                           "child_maps": children})
    scored.sort(key=lambda item: (-item["score"], item["path"]))
    # At most three semantic routes are enough to represent an intentionally
    # cross-cutting task. This bounds map reads, not what code may be accessed.
    selected = scored[:2]
    seen = set()
    unique_children = []
    for match in selected:
        for child in match["child_maps"]:
            if child not in seen:
                seen.add(child)
                unique_children.append(child)
    # Child-map identifiers in the canonical map are intentionally short.
    # Resolve them deterministically; do not probe the filesystem.
    unique_children = [
        c if "/" in c else "docs/design/repository_maps/%s.yaml" % c
        for c in unique_children
    ]
    route_paths = []
    child_details = []
    for child in unique_children:
        absolute = os.path.join(str(root), child)
        detail = {"map": child, "paths": []}
        if os.path.isfile(absolute):
            with open(absolute, "r", encoding="utf-8") as fh:
                text = fh.read()
            try:
                import yaml
                child_data = yaml.safe_load(text) or {}
            except (ImportError, Exception):
                try:
                    child_data = _simple_yaml_load(text)
                except ValueError:
                    child_data = {}
            base = child_data.get("path", "") if isinstance(child_data, dict) else ""
            candidates = []
            for entry in child_data.get("entry_points", []) or []:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    candidates.append(entry["path"])
            candidates.extend(p for p in (child_data.get("key_paths", []) or []) if isinstance(p, str))
            for candidate in candidates:
                joined = candidate if candidate.startswith(base + "/") else os.path.join(base, candidate)
                detail["paths"].append(joined.rstrip("/"))
                if joined.rstrip("/") not in route_paths:
                    route_paths.append(joined.rstrip("/"))
        child_details.append(detail)
    return {"matched": selected, "child_maps": child_details,
            "route_paths": route_paths, "map_path": loaded.get("map_path")}


# ---------------------------------------------------------------------------
# Literal search (single rg invocation)
# ---------------------------------------------------------------------------

def _rg_excludes():
    args = []
    for name in sorted(EXCLUDED_DIRS):
        args += ["--glob", "!%s/" % name]
    return args


def search_literal(root, text, route_paths=None, max_results=50, exhaustive=False):
    """One ``rg --fixed-strings --line-number --column --json`` run.

    ``max_results`` is a pagination/output bound on returned matches, not a
    ban on further results; ``exhaustive=True`` removes the bound.
    Runs as an argv list, never through a shell.
    """
    cmd = [
        "rg", "--fixed-strings", "--line-number", "--column", "--json",
    ] + _rg_excludes()
    if route_paths:
        cmd += [str(p) for p in route_paths]
    cmd += ["--", str(text)]
    proc = _run(cmd, cwd=root)
    matches = []
    truncated = False
    limit = None if exhaustive else int(max_results)
    # ``rg --json`` is newline-delimited events, not one JSON document.
    events = []
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "match":
            events.append(event.get("data", {}))
    for item in events:
        absolute_path = item.get("path", {}).get("text")
        sub = item.get("submatches", [])
        column = sub[0].get("col", 1) if sub else 1
        matches.append({
            "path": absolute_path,
            "line": item.get("line_number"),
            "column": column,
            "text": item.get("lines", {}).get("text", "").rstrip("\n"),
        })
        if limit is not None and len(matches) >= limit:
            truncated = True
            break
    return {
        "returncode": proc.returncode,
        "matches": matches,
        "truncated": truncated,
        "count": len(matches),
    }


# ---------------------------------------------------------------------------
# Symbol index (ctags if present, else AST over explicit .py files)
# ---------------------------------------------------------------------------

_AST_TOP_LEVEL_NAMES = {
    "FunctionDef": "function",
    "AsyncFunctionDef": "function",
    "ClassDef": "class",
    "Import": "import",
    "ImportFrom": "import",
}


def _collect_python_files(paths):
    files = []
    for entry in paths:
        entry = str(entry)
        if os.path.isfile(entry):
            if entry.endswith(".py"):
                files.append(entry)
        elif os.path.isdir(entry):
            for dirpath, dirnames, filenames in os.walk(entry):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                for filename in filenames:
                    if filename.endswith(".py"):
                        files.append(os.path.join(dirpath, filename))
    return files


def _ast_symbols(files):
    symbols = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, ValueError, SyntaxError):
            continue
        for node in tree.body:
            kind = _AST_TOP_LEVEL_NAMES.get(type(node).__name__)
            if kind is None:
                continue
            name = getattr(node, "name", None)
            if name is None and isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                name = ", ".join(names)
            if name:
                symbols.append({
                    "name": name,
                    "kind": kind,
                    "file": path,
                    "line": node.lineno,
                })
    return symbols


def _ctags_symbols(root, paths):
    ctags = shutil.which("ctags")
    if not ctags:
        return None
    targets = [str(p) for p in paths] if paths else ["."]
    cmd = [ctags, "--output-format=json", "--fields=+nK", "--extras=-F", "-f", "-"] + targets
    proc = _run(cmd, cwd=root)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    symbols = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        symbols.append({
            "name": record.get("name", record.get("_tag", "")),
            "kind": record.get("kind", record.get("_type", "")),
            "file": record.get("path", record.get("_filename", "")),
            "line": record.get("line", record.get("_line", 0)),
        })
    return symbols


def symbol_index(root, paths=None):
    """Index symbols via universal-ctags when available.

    Fallback: fast pure-Python AST indexing for explicitly passed .py
    files/dirs only (dirs walked with the standard generated exclusions).
    Never walks the whole repository root implicitly.
    """
    if paths is None:
        return {"backend": "none", "symbols": [],
                "error": "explicit paths required; implicit root walk disabled"}
    if shutil.which("ctags"):
        symbols = _ctags_symbols(root, paths)
        if symbols is not None:
            return {"backend": "ctags", "symbols": symbols}
    return {"backend": "ast", "symbols": _ast_symbols(_collect_python_files(paths))}


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def doctor(root):
    """Report required/optional tool availability plus environment hints."""
    tools = {}
    for name in ("rg", "ctags", "pyright-langserver", "clangd", "scip-python", "scip-clang"):
        tools[name] = shutil.which(name) is not None
    map_path = next(
        (os.path.join(str(root), c) for c in MAP_CANDIDATES
         if os.path.exists(os.path.join(str(root), c))),
        None,
    )
    compile_commands = [
        os.path.join(str(root), name)
        for name in ("compile_commands.json", "build/compile_commands.json")
        if os.path.exists(os.path.join(str(root), name))
    ]
    container = os.path.exists("/.dockerenv") or os.path.exists("/.containerenv")
    ros_distro = os.environ.get("ROS_DISTRO")
    recommendations = []
    if not tools["rg"]:
        recommendations.append("apt install ripgrep  # or: cargo install ripgrep")
    if not tools["ctags"]:
        recommendations.append("apt install ctags  # universal-ctags recommended")
    if not tools["pyright-langserver"]:
        recommendations.append("npm install -g pyright  # optional, python LSP")
    if not tools["clangd"]:
        recommendations.append("apt install clangd  # optional, C/C++ LSP")
    if not tools["scip-python"]:
        recommendations.append("pipx install scip-python  # optional precise Python index")
    if not tools["scip-clang"]:
        recommendations.append("install scip-clang  # optional precise C/C++ index")
    if map_path is None:
        recommendations.append(
            "create docs/design/repository_map.yaml with routes/path/anchors"
        )
    return {
        "root": str(root),
        "tools": {
            "required": {"rg": tools["rg"]},
            "optional": {k: v for k, v in tools.items() if k != "rg"},
        },
        "map": {"exists": map_path is not None, "path": map_path},
        "compile_commands": compile_commands,
        "environment": {
            "container": container,
            "ros_distro": ros_distro,
        },
        "recommendations": recommendations,
    }
