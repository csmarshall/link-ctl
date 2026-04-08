#!/bin/bash
unset TMOUT
set -euo pipefail
set -x

ENTITLEMENTS=$(mktemp)
cat > "$ENTITLEMENTS" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.usb</key>
    <true/>
</dict>
</plist>
PLIST

echo "=== Ad-hoc signing uvc-probe ==="
codesign -s - --force --entitlements "$ENTITLEMENTS" tools/uvc-probe
codesign -dv tools/uvc-probe

echo ""
echo "=== Ad-hoc signing usb-suspend ==="
codesign -s - --force --entitlements "$ENTITLEMENTS" tools/usb-suspend
codesign -dv tools/usb-suspend

rm "$ENTITLEMENTS"

echo ""
echo "=== Test uvc-probe WITHOUT sudo ==="
tools/uvc-probe get 9 0x1b 2

echo ""
echo "=== done ==="
