#!/bin/bash
# Install from a git checkout (no Homebrew tap needed). Same as running teamswatcher-setup.
#   ./install.sh [--upgrade] [--rebuild]
exec "$(cd "$(dirname "$0")" && pwd)/teamswatcher-setup" "$@"
