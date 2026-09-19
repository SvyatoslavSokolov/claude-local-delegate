#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=> Installing claude-local-delegate from $REPO_DIR"

# 1. Symlink ~/.claude/agents -> $REPO_DIR/agents
CLAUDE_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_DIR"
if [ -L "$CLAUDE_DIR/agents" ]; then
    rm "$CLAUDE_DIR/agents"
elif [ -e "$CLAUDE_DIR/agents" ]; then
    mv "$CLAUDE_DIR/agents" "$CLAUDE_DIR/agents_backup_$(date +%s)"
fi
ln -s "$REPO_DIR/agents" "$CLAUDE_DIR/agents"
echo "✅ Linked $CLAUDE_DIR/agents -> $REPO_DIR/agents"

# 2. Symlink ~/.gemini/config/GEMINI.md -> $REPO_DIR/agents/GEMINI.md
GEMINI_DIR="$HOME/.gemini/config"
mkdir -p "$GEMINI_DIR"
if [ -L "$GEMINI_DIR/GEMINI.md" ]; then
    rm "$GEMINI_DIR/GEMINI.md"
elif [ -e "$GEMINI_DIR/GEMINI.md" ]; then
    mv "$GEMINI_DIR/GEMINI.md" "$GEMINI_DIR/GEMINI.md.backup_$(date +%s)"
fi
ln -s "$REPO_DIR/agents/GEMINI.md" "$GEMINI_DIR/GEMINI.md"
echo "✅ Linked $GEMINI_DIR/GEMINI.md -> $REPO_DIR/agents/GEMINI.md"

# 3. Setup Codex symlink
CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"
if [ -L "$CODEX_DIR/local-delegate-agents" ]; then
    rm "$CODEX_DIR/local-delegate-agents"
elif [ -e "$CODEX_DIR/local-delegate-agents" ]; then
    mv "$CODEX_DIR/local-delegate-agents" "$CODEX_DIR/local-delegate-agents_backup_$(date +%s)"
fi
ln -s "$REPO_DIR/agents" "$CODEX_DIR/local-delegate-agents"
echo "✅ Linked $CODEX_DIR/local-delegate-agents -> $REPO_DIR/agents"

# 4. Apply Codex settings
if command -v python3 &> /dev/null; then
    echo "=> Registering settings into Codex..."
    python3 "$REPO_DIR/adapters/codex/install_codex.py" --apply
else
    echo "⚠️  python3 not found. Skipping Codex registration."
fi

# 5. Instructions for MCP registration
echo ""
echo "🎉 Installation complete!"
echo ""
echo "=== IMPORTANT: MCP CONFIGURATION ==="
echo "If you are moving this repo to a NEW computer, ensure you register the MCP servers."
echo ""
echo "For Claude Code (~/.claude.json):"
echo '{'
echo '  "mcpServers": {'
echo '    "claude-local-delegate": {'
echo '      "command": "python3",'
echo '      "args": ["'"$REPO_DIR/server.py"'"]'
echo '    },'
echo '    "code-nav": {'
echo '      "command": "python3",'
echo '      "args": ["'"$REPO_DIR/code_nav_server.py"'"]'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "For Google Antigravity (~/.gemini/config/mcp_config.json):"
echo '{'
echo '  "mcpServers": {'
echo '    "claude-local-delegate": {'
echo '      "command": "python3",'
echo '      "args": ["'"$REPO_DIR/server.py"'"]'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "Note: The settings profile (~/.claude/vllm.delegate.settings.json) contains secrets (API keys for vLLM/LiteLLM)."
echo "You must manually copy it via a secure channel (e.g. SCP or Age encryption) to new machines."
