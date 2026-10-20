#!/bin/bash
# Builds TeamsWatcher.app (Apple Silicon, macOS 14.2+). Needs Xcode Command Line Tools.
# Normally run by install.sh, which also installs the app.
set -euo pipefail
cd "$(dirname "$0")"

APP="build/TeamsWatcher.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 \
  -target arm64-apple-macos14.2 \
  -framework AppKit -framework CoreAudio \
  -o "$APP/Contents/MacOS/TeamsWatcher" \
  main.swift

cp Info.plist "$APP/Contents/Info.plist"

# Fingerprint of the source, so install.sh can skip rebuilds (and keep macOS permissions).
cat main.swift Info.plist | shasum -a 256 | cut -c1-16 > "$APP/Contents/Resources/source-hash"

codesign --force --sign - "$APP"

echo "Built $APP"
