#!/bin/bash
# Builds TeamsWatcher.app (Apple Silicon, macOS 14.2+). Needs Xcode Command Line Tools.
# Normally run by install.sh / teamswatcher-setup, which also installs the app.
set -euo pipefail
cd "$(dirname "$0")"

APP="build/TeamsWatcher.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 \
  -target arm64-apple-macos14.2 \
  -framework AppKit -framework CoreAudio -framework UserNotifications -framework SwiftUI \
  -o "$APP/Contents/MacOS/TeamsWatcher" \
  main.swift Settings.swift

cp Info.plist "$APP/Contents/Info.plist"

# App icon: scale AppIcon.png (1024x1024) into an iconset and compile it to .icns.
ICONSET="build/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" AppIcon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) AppIcon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# Fingerprint of the source, so setup can skip rebuilds (and keep macOS permissions).
cat main.swift Settings.swift Info.plist AppIcon.png | shasum -a 256 | cut -c1-16 > "$APP/Contents/Resources/source-hash"

codesign --force --sign - "$APP"

echo "Built $APP"
