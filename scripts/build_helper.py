#!/usr/bin/env python3
"""Build and verify one Open Computer Use Agent helper payload (build time only).

    python3 scripts/build_helper.py stage                    # patched source only
    python3 scripts/build_helper.py build darwin-arm64       # ad hoc signed unless told otherwise
    python3 scripts/build_helper.py build linux-x64
    python3 scripts/build_helper.py verify darwin-arm64 [--require-signed]

Every target ends as `build/payload/<target>/` holding the executable, LICENSE, NOTICE,
pin.json and a manifest.json that records the pin, the patch hashes and a SHA-256 of every
file. `scripts/build_brxt.py` packs those folders into the extension bundle.

This is a port of the build script Biorouter used for its earlier Open Computer Use helper,
with the new name, the new bundle identifier and a neutral icon.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor/open-computer-use"
PIN = json.loads((VENDOR / "pin.json").read_text(encoding="utf-8"))
BUILD = Path(os.environ.get("OCUA_BUILD_DIR") or ROOT / "build").resolve()
STAGE = BUILD / "source.noindex"
PAYLOADS = BUILD / "payload"
APP_NAME = PIN["app_name"]
TEAM_ID = "F3YYBXAFJ8"
SIGN_IDENTITY = f"Developer ID Application: University of California at San Francisco ({TEAM_ID})"
ICON_FILE = "OpenComputerUseAgent.icns"
CURSOR_FILE = "opencomputeruseagent-cursor.png"

_spec = importlib.util.spec_from_file_location("vendor_source", Path(__file__).with_name("vendor_source.py"))
vendor_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor_source)
_spec = importlib.util.spec_from_file_location("check_brand", Path(__file__).with_name("check_brand.py"))
check_brand = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_brand)


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patches():
    return [{"path": p.name, "sha256": digest(p)} for p in sorted((VENDOR / "patches").glob("*.patch"))]


def payload_files(directory):
    result = []
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink in helper payload: {path}")
        if path.is_file() and path != directory / "manifest.json":
            result.append({"path": path.relative_to(directory).as_posix(), "sha256": digest(path)})
    return result


def executable(target):
    if target.startswith("darwin-"):
        return f"{APP_NAME}.app/Contents/MacOS/ocu"
    return "ocu.exe" if target.startswith("win32-") else "ocu"


def check_binary(path, target):
    data = path.read_bytes()[:4096]
    valid = False
    if target.startswith("darwin-") and data[:4] == b"\xcf\xfa\xed\xfe":
        expected = 0x0100000C if target.endswith("arm64") else 0x01000007
        valid = struct.unpack_from("<I", data, 4)[0] == expected
    elif target.startswith("linux-"):
        valid = data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == (183 if target.endswith("arm64") else 62)
    elif target == "win32-x64" and data[:2] == b"MZ":
        offset = struct.unpack_from("<I", data, 60)[0]
        valid = data[offset:offset + 6] == b"PE\0\0\x64\x86"
    if not valid:
        raise ValueError(f"Native helper architecture does not match {target}: {path}")


def write_manifest(directory, target):
    manifest = {key: PIN[key] for key in ("schema_version", "upstream_commit", "upstream_version", "patch_revision")}
    manifest.update(target=target, executable=executable(target), args=["mcp"], patches=patches(),
                    files=payload_files(directory), linux_dependencies=PIN["linux_dependencies"])
    if target.startswith("darwin-"):
        manifest["bundle_identifier"] = PIN["bundle_identifier"]
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify(directory, target, signed=False, team_signed=False):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for key in ("schema_version", "upstream_commit", "upstream_version", "patch_revision"):
        if manifest.get(key) != PIN[key]:
            raise ValueError(f"Helper {key} does not match the source pin")
    if manifest.get("target") != target or manifest.get("executable") != executable(target):
        raise ValueError(f"Stale or foreign helper payload: expected {target}")
    if manifest.get("patches") != patches():
        raise ValueError("Helper patches changed; rebuild the helper")
    if manifest.get("files") != payload_files(directory):
        raise ValueError("Helper payload was modified, incomplete, or contains unrecorded files")
    if manifest.get("args") != ["mcp"]:
        raise ValueError("Unexpected helper launch arguments")
    check_binary(directory / executable(target), target)
    if os.name != "nt" and not target.startswith("win32-") and not os.access(directory / executable(target), os.X_OK):
        raise ValueError("Helper is not executable")
    if signed and target.startswith("darwin-"):
        app = directory / f"{APP_NAME}.app"
        run(["codesign", "--verify", "--strict", "--deep", app])
        info = subprocess.check_output(["codesign", "-dv", "--verbose=4", str(app)], stderr=subprocess.STDOUT).decode()
        if "Identifier=" + PIN["bundle_identifier"] not in info:
            raise ValueError("Helper is not signed with the Open Computer Use Agent identifier")
        if team_signed and f"TeamIdentifier={TEAM_ID}" not in info:
            raise ValueError("Helper is not signed with the release team identity")
    return manifest


def remove_tree(path):
    """`shutil.rmtree` that can also delete a git checkout on Windows (read-only pack files)."""
    def drop_readonly(func, target, _exception):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=drop_readonly)
    else:
        shutil.rmtree(path, onerror=lambda f, target, _e: drop_readonly(f, target, None))


def stage(source=None):
    """Stage the upstream tree and apply the reviewed patches to a throwaway copy.

    The vendored tree is pristine and verified in both directions before it becomes build
    input. The staged copy gets its own `git init`: `git apply` resolves against the repository
    containing the working directory, and inside this repository's ignored `build/` folder
    every new file hunk was silently dropped (measured in Biorouter's earlier vendoring).
    """
    STAGE.parent.mkdir(parents=True, exist_ok=True)
    if STAGE.exists():
        remove_tree(STAGE)
    if source:
        run(["git", "clone", "--no-checkout", source, STAGE])
        run(["git", "checkout", "--detach", PIN["upstream_commit"]], cwd=STAGE)
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=STAGE).decode().strip()
        if actual != PIN["upstream_commit"]:
            raise ValueError("Upstream source pin mismatch")
    else:
        vendor_source.verify()
        shutil.copytree(vendor_source.SOURCE, STAGE)
        run(["git", "init", "--quiet"], cwd=STAGE)
    for patch in sorted((VENDOR / "patches").glob("*.patch")):
        run(["git", "apply", "--check", patch], cwd=STAGE)
        run(["git", "apply", patch], cwd=STAGE)
    problems = check_brand.check_tree(STAGE)
    if problems:
        raise ValueError("Patched source fails the brand check:\n  " + "\n  ".join(problems[:20]))
    return STAGE


def png(width, height, pixel):
    """Encode an RGBA image given as pixel(x, y) -> (r, g, b, a)."""
    rows = []
    for y in range(height):
        row = bytearray(b"\0")
        for x in range(width):
            row.extend(pixel(x, y))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def inside_polygon(polygon, x, y):
    crossings = 0
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            crossings += 1
    return crossings % 2 == 1


# The pointer outline, in a 126 by 126 box with the tip at (60, 56) (AppKit's (60, 70)).
POINTER = [(60, 56), (60.7, 77.8), (66.1, 73.1), (69.1, 78.2), (72.4, 76.2), (69.4, 71.2), (77.8, 70.4)]


def draw_cursor(path):
    """The software cursor: a small dark pointer with a light rim, drawn here, not extracted."""
    fill, edge, samples = (42, 37, 32), (244, 240, 230), 4
    left, right = min(int(x) for x, _ in POINTER), max(int(x) + 1 for x, _ in POINTER)
    top, bottom = min(int(y) for _, y in POINTER), max(int(y) + 1 for _, y in POINTER)
    rim = [(x + (x - 66) * 0.12, y + (y - 69) * 0.12) for x, y in POINTER]

    def pixel(x, y):
        if not (left - 2 <= x <= right + 2 and top - 2 <= y <= bottom + 2):
            return (0, 0, 0, 0)
        hits_fill = hits_rim = 0
        for sy in range(samples):
            for sx in range(samples):
                px, py = x + (sx + 0.5) / samples, y + (sy + 0.5) / samples
                if inside_polygon(POINTER, px, py):
                    hits_fill += 1
                elif inside_polygon(rim, px, py):
                    hits_rim += 1
        total = hits_fill + hits_rim
        if not total:
            return (0, 0, 0, 0)
        color = tuple(round((fill[c] * hits_fill + edge[c] * hits_rim) / total) for c in range(3))
        return color + (round(255 * total / (samples * samples)),)
    path.write_bytes(png(126, 126, pixel))


def draw_icon(path):
    """A neutral app icon: a slate rounded square, a window outline and a pointer.

    Deliberately unlike Biorouter's monogram, so the two apps can be told apart in System
    Settings' Accessibility and Screen Recording lists.
    """
    def image(size):
        scale = size / 256.0
        radius = 56 * scale
        pointer = [((x - 60) * 4.2 * scale + 150 * scale, (y - 56) * 4.2 * scale + 118 * scale) for x, y in POINTER]

        def in_rounded(x, y, x0, y0, x1, y1, r):
            cx = min(max(x, x0 + r), x1 - r)
            cy = min(max(y, y0 + r), y1 - r)
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and x0 <= x <= x1 and y0 <= y <= y1

        def pixel(x, y):
            px, py = x + 0.5, y + 0.5
            m = 12 * scale
            if not in_rounded(px, py, m, m, size - m, size - m, radius):
                return (0, 0, 0, 0)
            if inside_polygon(pointer, px, py):
                return (250, 248, 242, 255)
            w0, w1, h0, h1 = 52 * scale, 204 * scale, 64 * scale, 176 * scale
            stroke = 7 * scale
            in_window = w0 <= px <= w1 and h0 <= py <= h1
            in_hollow = w0 + stroke <= px <= w1 - stroke and h0 + stroke * 3 <= py <= h1 - stroke
            if in_window and not in_hollow:
                return (126, 196, 186, 255)
            shade = int(56 + 30 * (py / size))
            return (shade - 12, shade + 6, shade + 22, 255)
        return png(size, size, pixel)

    entries = [(b"ic07", image(128)), (b"ic08", image(256))]
    body = b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in entries)
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def build(target, source, signing_identity):
    checkout = stage(source)
    destination = PAYLOADS / target
    # A failed build must never leave an older payload eligible for packaging.
    if destination.exists():
        remove_tree(destination)
    destination.mkdir(parents=True)
    binary = destination / executable(target)
    binary.parent.mkdir(parents=True, exist_ok=True)
    jobs = os.environ.get("OCUA_BUILD_JOBS", "4")
    if target.startswith("darwin-"):
        if platform.system() != "Darwin":
            raise ValueError("macOS helpers must be built on macOS with Swift 6.2 or later")
        arch = "arm64" if target.endswith("arm64") else "x86_64"
        scratch = BUILD / ("swift-" + target + ".noindex")
        args = ["swift", "build", "-c", "release", "--jobs", jobs, "--triple",
                f"{arch}-apple-macosx{PIN['minimum_macos']}", "--scratch-path", scratch]
        run(args + ["--product", "OpenComputerUse"], cwd=checkout)
        built = Path(subprocess.check_output([str(a) for a in args + ["--show-bin-path"]], cwd=checkout).decode().strip())
        shutil.copy2(built / "OpenComputerUse", binary)
        contents = binary.parent.parent
        resources = contents / "Resources"
        resources.mkdir()
        draw_cursor(resources / CURSOR_FILE)
        draw_icon(resources / ICON_FILE)
        info = dict(CFBundleIdentifier=PIN["bundle_identifier"], CFBundleName=APP_NAME,
                    CFBundleDisplayName=APP_NAME, CFBundleExecutable="ocu",
                    CFBundlePackageType="APPL", CFBundleShortVersionString=PIN["upstream_version"],
                    CFBundleVersion="1", LSMinimumSystemVersion=PIN["minimum_macos"], LSUIElement=True,
                    NSHighResolutionCapable=True, NSPrincipalClass="NSApplication",
                    CFBundleIconFile=ICON_FILE)
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        binary.chmod(0o755)
        identity = signing_identity or "-"
        args = ["codesign", "--force", "--sign", identity]
        if identity != "-":
            args += ["--options", "runtime", "--timestamp"]
        run(args + [contents.parent])
    else:
        app = "OpenComputerUseWindows" if target == "win32-x64" else "OpenComputerUseLinux"
        env = dict(os.environ, GOOS="windows" if target == "win32-x64" else "linux",
                   GOARCH="arm64" if target.endswith("arm64") else "amd64", CGO_ENABLED="0",
                   GOMAXPROCS=jobs)
        run(["go", "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w", "-o", binary, "."],
            cwd=checkout / "apps" / app, env=env)
        binary.chmod(0o755)
        if target == "win32-x64" and os.environ.get("WINDOWS_CERTIFICATE_FILE"):
            signed_binary = binary.with_suffix(".signed.exe")
            run(["osslsigncode", "sign", "-pkcs12", os.environ["WINDOWS_CERTIFICATE_FILE"],
                 "-readpass", os.environ["WINDOWS_SIGN_PASSWORD_FILE"], "-h", "sha256",
                 "-ts", "http://timestamp.digicert.com", "-in", binary, "-out", signed_binary])
            signed_binary.replace(binary)
    for name in ("LICENSE", "NOTICE", "pin.json"):
        shutil.copy2(VENDOR / name, destination / name)
    write_manifest(destination, target)
    verify(destination, target, signed=target.startswith("darwin-"))
    print(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("build", "verify", "stage", "resign", "remanifest"))
    parser.add_argument("target", nargs="?", choices=PIN["targets"])
    parser.add_argument("--source", help="a local upstream clone; the pinned commit is checked out afresh")
    parser.add_argument("--directory", type=Path, help="the payload folder to verify (default build/payload/<target>)")
    parser.add_argument("--signing-identity", help="codesign identity; ad hoc ('-') when omitted")
    parser.add_argument("--require-signed", action="store_true", help="require the release team signature")
    args = parser.parse_args()
    if args.action == "stage":
        print(stage(args.source))
        return
    if not args.target:
        parser.error("a target is required")
    if args.action == "build":
        build(args.target, args.source, args.signing_identity)
    elif args.action == "resign":
        # Re-sign an existing macOS payload (the release Mac signs with the Developer ID here),
        # then rewrite its manifest, since the signature changes the app's files.
        directory = args.directory or PAYLOADS / args.target
        if not args.target.startswith("darwin-") or not args.signing_identity:
            parser.error("resign takes a darwin target and --signing-identity")
        run(["codesign", "--force", "--options", "runtime", "--timestamp", "--sign",
             args.signing_identity, directory / f"{APP_NAME}.app"])
        write_manifest(directory, args.target)
        verify(directory, args.target, signed=True, team_signed=True)
        print(directory)
    elif args.action == "remanifest":
        # After notarization: the stapled ticket is a new file in the app, so the manifest is
        # rewritten, then the payload is verified with the release team's signature required.
        directory = args.directory or PAYLOADS / args.target
        write_manifest(directory, args.target)
        verify(directory, args.target, signed=True, team_signed=args.require_signed)
        print(directory)
    else:
        verify(args.directory or PAYLOADS / args.target, args.target,
               signed=args.target.startswith("darwin-"), team_signed=args.require_signed)
        print(f"Verified Open Computer Use Agent helper {args.target}")


if __name__ == "__main__":
    main()
