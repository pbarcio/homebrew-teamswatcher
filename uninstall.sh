#!/bin/bash
# Removes TeamsWatcher, and optionally ownscribe. Your transcripts in ~/ownscribe are always kept.
# Installed by Homebrew as `teamswatcher-uninstall`; run it before `brew uninstall teamswatcher`.
set -euo pipefail

APP_NAME="TeamsWatcher"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if pgrep -f "$HOME/.local/bin/ownscribe" >/dev/null 2>&1; then
  echo "ownscribe is recording or processing right now. Re-run when it has finished." >&2
  exit 1
fi

echo "Removing $APP_NAME..."
if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
  osascript -e "quit app \"$APP_NAME\"" >/dev/null 2>&1 || pkill -x "$APP_NAME" || true
  sleep 1
fi
osascript -e "tell application \"System Events\" to delete login item \"$APP_NAME\"" >/dev/null 2>&1 || true
rm -rf "/Applications/$APP_NAME.app" "$HOME/Applications/$APP_NAME.app"
rm -rf "$HOME/.config/teamswatcher" "$HOME/Library/Logs/TeamsWatcher"
# The certificate setup created for signing the app (it's only used for TeamsWatcher).
security delete-identity -c "TeamsWatcher Local Signing" >/dev/null 2>&1 || true
echo "$APP_NAME removed. You can also remove its entries under Privacy & Security in System Settings."

read -r -p "Also uninstall ownscribe and its config? [y/N] " answer
if [[ "$answer" =~ ^[Yy] ]]; then
  uv tool uninstall ownscribe >/dev/null 2>&1 || true
  rm -rf "$HOME/.config/ownscribe"
  echo "ownscribe removed."
  echo "Downloaded models remain in ~/.cache/huggingface (shared with other tools); delete them there if you like."
fi

echo "Transcripts in ~/ownscribe were left in place."
if command -v brew >/dev/null 2>&1 && brew list --formula teamswatcher >/dev/null 2>&1; then
  echo "To finish, run: brew uninstall teamswatcher"
fi
