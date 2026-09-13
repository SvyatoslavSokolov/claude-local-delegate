#!/usr/bin/env bash
set -euo pipefail

# Run on the host or inside the development container. Package-manager work is
# explicit so this script is suitable for a Dockerfile RUN layer as well.
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ripgrep universal-ctags nodejs npm
fi

if ! command -v pyright-langserver >/dev/null 2>&1; then
  npm install --global pyright
fi

echo "Required: rg. Recommended: universal-ctags + pyright."
echo "For ROS 2 C++ also install clangd and build with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON."
echo "SCIP indexers are optional for persistent precise indexes, not required for interactive navigation."
