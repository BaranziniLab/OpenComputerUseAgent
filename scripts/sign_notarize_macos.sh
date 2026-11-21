#!/bin/bash
# Sign the macOS helper apps with the Developer ID, notarize them, staple the tickets, and
# rewrite each payload's manifest (the signature and the ticket change the app's files).
#
#   scripts/sign_notarize_macos.sh [build/payload]
#
# Runs on the release Mac only. Credentials are read the way Biorouter's release does it:
# APPLE_ID and APPLE_APP_SPECIFIC_PASSWORD from the environment, else the Keychain item
# `biorouter-notarization`. Nothing is written to this repository, and no certificate or
# password belongs in it or in its Actions secrets.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PAYLOADS=${1:-$ROOT/build/payload}
TEAM_ID=F3YYBXAFJ8
IDENTITY="Developer ID Application: University of California at San Francisco ($TEAM_ID)"
APP="Open Computer Use Agent.app"
die() { echo "sign_notarize_macos: $*" >&2; exit 1; }

[ "$(uname)" = Darwin ] || die "run this on the release Mac"
security find-identity -v -p codesigning | grep -q "$IDENTITY" || die "the signing identity is not in this Mac's Keychain"
if [ -z "${APPLE_ID:-}" ]; then
  APPLE_ID="$(security find-generic-password -s biorouter-notarization -a APPLE_ID -w 2>/dev/null || true)"
fi
if [ -z "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]; then
  APPLE_APP_SPECIFIC_PASSWORD="$(security find-generic-password -s biorouter-notarization -a APPLE_APP_SPECIFIC_PASSWORD -w 2>/dev/null || true)"
fi
[ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ] || die "no notarization credentials (environment or Keychain item biorouter-notarization)"

for target in darwin-arm64 darwin-x64; do
  dir="$PAYLOADS/$target"
  [ -d "$dir/$APP" ] || die "missing $dir/$APP; build it with scripts/build_helper.py build $target"
  echo "== $target: sign"
  codesign --force --options runtime --timestamp --sign "$IDENTITY" "$dir/$APP"
  codesign --verify --strict --deep "$dir/$APP"
  echo "== $target: notarize"
  work=$(mktemp -d)
  ditto -c -k --keepParent "$dir/$APP" "$work/helper.zip"
  xcrun notarytool submit "$work/helper.zip" --apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" \
    --team-id "$TEAM_ID" --wait
  rm -rf "$work"
  echo "== $target: staple"
  xcrun stapler staple "$dir/$APP"
  xcrun stapler validate "$dir/$APP"
  spctl -a -vv -t exec "$dir/$APP"
  python3 "$ROOT/scripts/build_helper.py" remanifest "$target" --directory "$dir" --require-signed
done
