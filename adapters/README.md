# adapters/

Integration adapters for external agent ecosystems.

## Purpose

`adapters/` isolates everything that talks to an *external* CLI or agent toolchain,
keeping the supervisor (Tier 1) core free of per-ecosystem specifics. Each subpackage
is one self-contained integration:

| Package | Ecosystem | What it does |
|---------|-----------|--------------|
| `adapters/agy/` | Google Antigravity (`agy` CLI) | `AgyDelegateManager` in `agy_delegate.py` spawns, tracks, and collects results from `agy` tasks running headless (non-interactive) mode. |
| `adapters/codex/` | OpenAI Codex CLI | `install_codex.py` — a reviewable, additive installer that registers the delegate tools into a Codex install (no credentials copied). |

The subpackages are importable packages (`__init__.py` is present in each).

## Backward compatibility: root `agy_delegate.py`

The top-level `agy_delegate.py` is a thin re-export shim:

```python
from adapters.agy.agy_delegate import (
    DEFAULT_STATE_DIR,
    DEFAULT_MODEL,
    AGY_BIN,
    AgyDelegateManager,
)
```

Reason: before the adapter was moved into `adapters/agy/`, scripts, tooling, and
migrated configs imported it as a top-level module (`import agy_delegate` /
`from agy_delegate import AgyDelegateManager`). Rather than breaking those imports,
the root file re-exports the public names from the new location. Existing code keeps
working unchanged; new code should import from `adapters.agy.agy_delegate` directly.

The shim exports exactly the public surface (`DEFAULT_STATE_DIR`, `DEFAULT_MODEL`,
`AGY_BIN`, `AgyDelegateManager`) and nothing more — keep any future changes to the
public API in sync in both places.

## Extension contract: adding a new adapter

To integrate another CLI or toolchain:

1. **Create a subpackage** `adapters/<name>/` with an (empty) `__init__.py`.
   The name should be a short, unambiguous CLI identifier (e.g. `agy`, `codex`).
2. **Put all ecosystem-specific logic inside the package.** At minimum provide a
   manager/runner module that owns: spawning the external process, writing run state
   to a per-adapter directory under `~/.claude-local-delegate/`, and collecting the
   result. Follow the `AgyDelegateManager` shape (state dir, model constant,
   binary overridable via env var, e.g. `AGY_BIN`).
3. **Do not depend on other adapters.** Adapters must be independent; any shared
   helper belongs in the supervisor core, not in a sibling adapter.
4. **Add a root re-export shim only if needed.** If existing code already imports
   the adapter as a top-level module, add a `<name>_delegate.py` shim at the repo
   root that re-exports the public names (see `agy_delegate.py` above). New
   integrations do not need a shim.
5. **Expose one stable public surface** (a small set of constants + one manager
   class), and keep it importable as `adapters.<name>.<module>`.

Verification: import the public surface from the new location
(`from adapters.<name>.<module> import ...`) and, if a shim exists, confirm it
resolves to the same objects.