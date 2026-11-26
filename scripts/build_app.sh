#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/AgentPulse.app"
ICONSET="$DIST/AppIcon.iconset"
BUNDLE_ID="${AGENTPULSE_BUNDLE_ID:-dev.agentpulse.app}"
VERSION="0.4.0"
BUILD_NUMBER="7"

mkdir -p "$DIST"

echo "==> [1/4] Building Swift release binary..."
cd "$ROOT/macos"
swift build -c release
BIN_PATH="$ROOT/macos/.build/release/AgentPulse"

echo "==> [2/4] Generating AppIcon.icns..."
rm -rf "$ICONSET"
swift "$ROOT/scripts/generate_icon.swift" "$ICONSET"
iconutil -c icns "$ICONSET" -o "$DIST/AppIcon.icns"
rm -rf "$ICONSET"

echo "==> [3/4] Assembling AgentPulse.app..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN_PATH" "$APP/Contents/MacOS/AgentPulse"
chmod +x "$APP/Contents/MacOS/AgentPulse"
cp "$ROOT/engine/collector.py" "$APP/Contents/Resources/collector.py"
cp "$ROOT/engine/usage_collector.py" "$APP/Contents/Resources/usage_collector.py"
cp "$ROOT/engine/pricing.py" "$APP/Contents/Resources/pricing.py"
cp "$ROOT/engine/ledger.py" "$APP/Contents/Resources/ledger.py"
cp "$ROOT/engine/quota_collector.py" "$APP/Contents/Resources/quota_collector.py"
cp "$ROOT/engine/pricing.json" "$APP/Contents/Resources/pricing.json"
cp "$ROOT/engine/pricing_overrides.json" "$APP/Contents/Resources/pricing_overrides.json"
cp "$ROOT/scripts/import_tokei.py" "$APP/Contents/Resources/import_tokei.py"
chmod +x "$APP/Contents/Resources/collector.py" "$APP/Contents/Resources/quota_collector.py" "$APP/Contents/Resources/import_tokei.py"
cp "$DIST/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleDisplayName</key>
    <string>AgentPulse</string>
    <key>CFBundleExecutable</key>
    <string>AgentPulse</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>AgentPulse</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleVersion</key>
    <string>${BUILD_NUMBER}</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo "==> [4/4] Ad-hoc signing AgentPulse.app..."
codesign --force --deep --sign - "$APP"
file "$APP/Contents/MacOS/AgentPulse"
echo "Built successfully: $APP (v${VERSION}, build ${BUILD_NUMBER}, bundle ${BUNDLE_ID})"
