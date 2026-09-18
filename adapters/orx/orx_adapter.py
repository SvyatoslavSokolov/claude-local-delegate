"""OpenResearch (orx) client adapter.

Provides programmatic access to alphaXiv, arXiv, OpenAlex, and bioRxiv
via the local `orx` CLI.
"""

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional


def find_orx_bin() -> Optional[str]:
    """Find the path to the `orx` executable.

    Checks:
    1. $ORX_BIN environment variable.
    2. System PATH.
    3. ~/.cargo/bin/orx.
    4. ~/.local/bin/orx.
    """
    env_bin = os.environ.get("ORX_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    which_bin = shutil.which("orx")
    if which_bin:
        return which_bin

    for candidate in ("~/.cargo/bin/orx", "~/.local/bin/orx"):
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

    return None


def orx_version() -> Optional[str]:
    """Return the installed version of `orx` or None if not found."""
    binary = find_orx_bin()
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def discover_papers(
    query: str,
    mode: str = "keyword",
    limit: int = 5,
    published_after: Optional[str] = None,
    published_before: Optional[str] = None,
    prioritize: Optional[str] = None,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """Search for scientific papers across arXiv, OpenAlex, or bioRxiv.

    Args:
        query: Search keywords or semantic description.
        mode: Retrieval primitive ("keyword", "embedding", "openalex", "biorxiv").
        limit: Max number of papers to return.
        published_after: YYYY-MM-DD date lower bound.
        published_before: YYYY-MM-DD date upper bound.
        prioritize: Ranking priority ("default", "recency", "historical", "popular").
        timeout: Subprocess timeout in seconds.

    Returns:
        List of paper dicts containing id, title, abstract, publicationDate, etc.
    """
    binary = find_orx_bin()
    if not binary:
        raise FileNotFoundError(
            "OpenResearch CLI (`orx`) was not found on PATH, ~/.cargo/bin/orx, or $ORX_BIN.\n"
            "Install it via: curl -LsSf https://openresearch.sh/install.sh | sh"
        )

    valid_modes = ("keyword", "embedding", "openalex", "biorxiv")
    if mode not in valid_modes:
        raise ValueError(f"Invalid discovery mode: '{mode}'. Must be one of {valid_modes}")

    cmd = [binary, "--no-telemetry", "discover", mode, query, "--limit", str(max(1, limit))]

    if published_after:
        cmd.extend(["--published-after", published_after])
    if published_before:
        cmd.extend(["--published-before", published_before])
    if prioritize:
        cmd.extend(["--prioritize", prioritize])

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"orx discover failed (exit {proc.returncode}): {err}")

    try:
        data = json.loads(proc.stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse orx JSON output: {e}\nRaw output: {proc.stdout[:400]}")


def fetch_paper(
    paper_id: str,
    full_text: bool = False,
    source: Optional[str] = None,
    timeout: int = 40,
) -> str:
    """Fetch structured content or full text of a paper by arXiv ID or DOI.

    Args:
        paper_id: arXiv identifier (e.g. "2608.14707") or DOI.
        full_text: If True, passes `--full` to fetch raw text.
        source: Optional source override ("alphaxiv", "openalex", "biorxiv").
        timeout: Subprocess timeout in seconds.

    Returns:
        Paper text content.
    """
    binary = find_orx_bin()
    if not binary:
        raise FileNotFoundError(
            "OpenResearch CLI (`orx`) was not found on PATH or ~/.cargo/bin/orx.\n"
            "Install it via: curl -LsSf https://openresearch.sh/install.sh | sh"
        )

    cmd = [binary, "--no-telemetry", "paper", str(paper_id)]
    if full_text:
        cmd.append("--full")
    if source:
        cmd.extend(["--source", source])

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"orx paper failed (exit {proc.returncode}): {err}")

    return proc.stdout.strip()
