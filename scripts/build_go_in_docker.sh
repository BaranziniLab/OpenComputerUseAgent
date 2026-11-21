#!/bin/sh
# Build the Linux and Windows helpers with the pinned Go toolchain in Docker, from any host.
#   scripts/build_go_in_docker.sh [build dir name] [targets...]
# Output: <repo>/<build dir name>/payload/<target>/ (default build dir: build).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME=${1:-build}
[ $# -gt 0 ] && shift
TARGETS=${*:-"linux-x64 linux-arm64 win32-x64"}
IMAGE=${OCUA_GO_IMAGE:-golang:1.26.8-bookworm}
docker run --rm -v "$ROOT:/work" -w /work -e OCUA_BUILD_DIR="/work/$NAME" -e OCUA_BUILD_JOBS=4 \
  -e TARGETS="$TARGETS" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$IMAGE" sh -c '
    set -e
    git config --global --add safe.directory "*"
    for t in $TARGETS; do
      echo "=== $t"
      python3 scripts/build_helper.py build "$t"
    done
    chown -R "$HOST_UID:$HOST_GID" "$OCUA_BUILD_DIR"
  '
