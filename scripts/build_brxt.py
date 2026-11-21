#!/usr/bin/env python3
"""Pack the extension and its helper payloads into one .brxt, and check a .brxt.

    python3 scripts/build_brxt.py build --payloads build/payload build-go/payload
    python3 scripts/build_brxt.py build --payloads dist/payloads --require-all
    python3 scripts/build_brxt.py check dist/opencomputeruseagent.brxt

The bundle carries every payload it is given under `payload/<target>/<version>/`, so a person
on a restricted network needs nothing after install beyond what `uv sync` does (and with no
dependencies and no build backend, `uv sync` needs no network either). The zip is
deterministic: sorted names, fixed times, no caches, no tests.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUDGET = 40 * 1024 * 1024
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
TOP_FILES = ("manifest.json", "README.md", "LICENSE", "NOTICE", "pyproject.toml", "uv.lock", "opencomputeruseagent.py")
NAME_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_spec = importlib.util.spec_from_file_location("build_helper", Path(__file__).with_name("build_helper.py"))
build_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_helper)


def version():
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["version"]


def find_payloads(sources):
    found = {}
    for source in sources:
        source = Path(source)
        for target in build_helper.PIN["targets"]:
            directory = source / target
            if (directory / "manifest.json").is_file():
                if target in found:
                    raise SystemExit(f"{target} is given twice ({found[target]} and {directory})")
                found[target] = directory
    return found


def entries(payloads):
    files = []
    for name in TOP_FILES:
        files.append((name, ROOT / name))
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files.append((path.relative_to(ROOT).as_posix(), path))
    ver = version()
    for target, directory in sorted(payloads.items()):
        build_helper.verify(directory, target, signed=target.startswith("darwin-"))
        for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
            if path.is_symlink():
                raise SystemExit(f"symlink in payload: {path}")
            if path.is_file():
                files.append((f"payload/{target}/{ver}/{path.relative_to(directory).as_posix()}", path))
    return files


def write_zip(destination, files):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files):
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = name.endswith("/ocu") or name.endswith("/ocu.exe") or "/Contents/MacOS/" in name
            info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes())


def check(path, require=()):
    """What `BrxtBundle::open` and the desktop's checks require, plus this bundle's own rules."""
    problems = []
    path = Path(path)
    if path.stat().st_size > BUDGET:
        problems.append(f"bundle is {path.stat().st_size} bytes, over the {BUDGET} byte budget")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for required, present in (("manifest.json", "manifest.json" in names),
                                  ("README.md", any(n.lower() == "readme.md" for n in names)),
                                  ("pyproject.toml", "pyproject.toml" in names),
                                  ("src/ directory", any(n.startswith("src/") for n in names))):
            if not present:
                problems.append(f"missing {required}")
        for name in names:
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or "\\" in name:
                problems.append(f"unsafe path {name}")
            if "__pycache__" in parts or name.endswith(".pyc") or name.startswith(("tests/", ".venv/")):
                problems.append(f"should not ship {name}")
            mode = (archive.getinfo(name).external_attr >> 16) & 0o170000
            if mode == 0o120000:
                problems.append(f"symlink {name}")
        manifest = json.loads(archive.read("manifest.json"))
        for key in ("name", "display_name", "description", "version", "entry_point", "repository"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                problems.append(f"manifest.json missing required field: {key}")
        if not isinstance(manifest.get("env_vars"), list):
            problems.append('manifest.json "env_vars" must be an array')
        if not NAME_RULE.match(manifest.get("name", "")) or manifest.get("name") == "computercontroller":
            problems.append(f"manifest name {manifest.get('name')!r} is not allowed")
        if manifest.get("entry_point") not in names:
            problems.append(f"entry point {manifest.get('entry_point')} is not in the bundle")
        targets = sorted({n.split("/")[1] for n in names if n.startswith("payload/") and n.count("/") >= 3})
        for target in targets:
            prefix = f"payload/{target}/{manifest['version']}/"
            if f"{prefix}manifest.json" not in names:
                problems.append(f"{target} payload has no manifest.json under {prefix}")
                continue
            payload_manifest = json.loads(archive.read(f"{prefix}manifest.json"))
            for entry in payload_manifest["files"]:
                member = prefix + entry["path"]
                if member not in names:
                    problems.append(f"{target}: {entry['path']} is missing")
                elif hashlib.sha256(archive.read(member)).hexdigest() != entry["sha256"]:
                    problems.append(f"{target}: {entry['path']} does not match its hash")
            for key in ("LICENSE", "NOTICE", "pin.json"):
                if prefix + key not in names:
                    problems.append(f"{target}: {key} is missing (MIT notice)")
        for target in require:
            if target not in targets:
                problems.append(f"{target} payload is missing")
    return problems, {"size": path.stat().st_size, "targets": targets, "entries": len(names)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--payloads", nargs="+", default=[str(build_helper.PAYLOADS)],
                   help="folders that hold <target>/ payload folders")
    b.add_argument("--require-all", action="store_true", help="fail unless all five targets are present")
    b.add_argument("--out", type=Path, default=DIST)
    c = sub.add_parser("check")
    c.add_argument("bundle", type=Path)
    c.add_argument("--require-all", action="store_true")
    args = parser.parse_args()
    require = build_helper.PIN["targets"] if args.require_all else ()
    if args.command == "build":
        payloads = find_payloads(args.payloads)
        if not payloads:
            raise SystemExit("no payloads found; build them with scripts/build_helper.py")
        ver = version()
        stable = args.out / "opencomputeruseagent.brxt"
        versioned = args.out / f"opencomputeruseagent-{ver}.brxt"
        write_zip(stable, entries(payloads))
        versioned.write_bytes(stable.read_bytes())
        problems, info = check(stable, require)
        sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (stable, versioned))
        (args.out / "SHA256SUMS").write_text(sums)
        print(json.dumps(dict(info, bundle=str(stable)), indent=2))
    else:
        problems, info = check(args.bundle, require)
        print(json.dumps(info, indent=2))
    if problems:
        for problem in problems:
            print("problem:", problem, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
