#!/bin/bash
# Cut an Open Computer Use Agent release on the release Mac, one phase at a time or all at once.
#
#   scripts/release.sh all 0.1.0        # bump, payloads, sign, verify, then a DRAFT release
#   scripts/release.sh bump 0.1.0       # write the version everywhere it lives
#   scripts/release.sh payloads 0.1.0   # build all five helpers (Go in Docker, Swift here)
#   scripts/release.sh sign 0.1.0       # Developer ID, notarize, staple (scripts/sign_notarize_macos.sh)
#   scripts/release.sh verify 0.1.0     # signatures, tickets, hashes, the bundle, an isolated install
#   scripts/release.sh draft 0.1.0      # gh release create --draft at the exact commit, after green CI
#   scripts/release.sh publish 0.1.0    # only after the macOS live drive is recorded for this build
#
# It mirrors Biorouter's own scripts/release.sh. `all` stops at the draft: publishing needs the
# recorded macOS live drive (README "Build and test"). Nothing secret is read from or written to
# this repository; signing uses this Mac's Keychain.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PHASE=${1:-}
VERSION=${2:-}
DIST="$ROOT/dist"
REPO="BaranziniLab/OpenComputerUseAgent"
die() { echo "release: $*" >&2; exit 1; }
log() { echo "== $*"; }
[ -n "$PHASE" ] && [ -n "$VERSION" ] || die "usage: scripts/release.sh <phase> <X.Y.Z>"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z"

current_version() { python3 -c "import json; print(json.load(open('manifest.json'))['version'])"; }
require_version() { [ "$(current_version)" = "$VERSION" ] || die "the tree is at $(current_version), not $VERSION; run bump first"; }

phase_bump() {
  python3 scripts/version.py bump "$VERSION"
  python3 scripts/version.py check
  uv lock
  log "bumped to $VERSION; commit it, push, and wait for CI before draft"
}

phase_payloads() {
  require_version
  python3 scripts/vendor_source.py --verify
  python3 scripts/check_vendored.py
  scripts/build_go_in_docker.sh build linux-x64 linux-arm64 win32-x64
  python3 scripts/build_helper.py build darwin-arm64
  python3 scripts/build_helper.py build darwin-x64
}

phase_sign() {
  require_version
  scripts/sign_notarize_macos.sh "$ROOT/build/payload"
}

phase_verify() {
  require_version
  for target in darwin-arm64 darwin-x64; do
    python3 scripts/build_helper.py verify "$target" --require-signed
    xcrun stapler validate "build/payload/$target/Open Computer Use Agent.app"
    spctl -a -vv -t exec "build/payload/$target/Open Computer Use Agent.app"
  done
  for target in linux-x64 linux-arm64 win32-x64; do
    python3 scripts/build_helper.py verify "$target"
  done
  python3 scripts/build_brxt.py build --payloads build/payload --require-all
  python3 scripts/build_brxt.py check "$DIST/opencomputeruseagent.brxt" --require-all
  python3 tests/test_helper_protocol.py build/payload/darwin-arm64
  BIN=${BIOROUTER_BIN:-$(dirname "$(command -v biorouter)")}
  ISO=$(mktemp -d "$HOME/ocua-release-XXXXXX")
  python3 tests/test_biorouter_install.py --bin "$BIN" --root "$ISO" --answer deny
  rm -rf "$ISO"
  git rev-parse HEAD > "$DIST/source_commit"
  python3 - "$VERSION" <<'PY'
import json, subprocess, sys
pin = json.load(open("vendor/open-computer-use/pin.json"))
provenance = {"version": sys.argv[1], "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
              "upstream_repository": pin["repository"], "upstream_commit": pin["upstream_commit"],
              "upstream_version": pin["upstream_version"], "patch_revision": pin["patch_revision"]}
open("dist/provenance.json", "w").write(json.dumps(provenance, indent=2) + "\n")
PY
  (cd "$DIST" && shasum -a 256 opencomputeruseagent.brxt "opencomputeruseagent-$VERSION.brxt" provenance.json > SHA256SUMS)
  log "verified; artifacts in $DIST"
}

phase_draft() {
  require_version
  local sha
  sha=$(git rev-parse HEAD)
  git fetch --quiet origin main
  [ "$sha" = "$(git rev-parse origin/main)" ] || die "HEAD is not origin/main; push and wait for CI first"
  [ "$(cat "$DIST/source_commit" 2>/dev/null)" = "$sha" ] || die "the artifacts were not built from $sha; run verify again"
  gh run list --repo "$REPO" --workflow ci.yml --commit "$sha" --status success --json databaseId --jq 'length' \
    | grep -qv '^0$' || die "no green CI run for $sha"
  gh release create "v$VERSION" --repo "$REPO" --draft --target "$sha" --title "OpenComputerUseAgent v$VERSION" \
    --notes "Open Computer Use Agent $VERSION (Open Computer Use $(python3 -c "import json; print(json.load(open('vendor/open-computer-use/pin.json'))['upstream_version'])"), patches $(python3 -c "import json; print(json.load(open('vendor/open-computer-use/pin.json'))['patch_revision'])"))." \
    "$DIST/opencomputeruseagent.brxt" "$DIST/opencomputeruseagent-$VERSION.brxt" "$DIST/SHA256SUMS" "$DIST/provenance.json"
  log "draft created; record the macOS live drive (scripts/macos_live_drive.py), then: scripts/release.sh publish $VERSION"
}

phase_publish() {
  [ -f "$ROOT/build/macos-live/$VERSION.json" ] || die "no recorded macOS live drive for $VERSION (build/macos-live/$VERSION.json)"
  python3 -c "import json,sys; sys.exit(0 if json.load(open('build/macos-live/$VERSION.json')).get('passed') else 1)" \
    || die "the recorded macOS live drive did not pass"
  gh release edit "v$VERSION" --repo "$REPO" --draft=false
}

case "$PHASE" in
  bump) phase_bump ;;
  payloads) phase_payloads ;;
  sign) phase_sign ;;
  verify) phase_verify ;;
  draft) phase_draft ;;
  publish) phase_publish ;;
  all) phase_bump; phase_payloads; phase_sign; phase_verify; phase_draft ;;
  *) die "unknown phase $PHASE" ;;
esac
