#!/usr/bin/env python3
"""Keep the extension's version and the engine pin in step.

    python3 scripts/version.py check          # fail if anything disagrees
    python3 scripts/version.py bump 0.1.1     # write the version everywhere it lives

The version lives in manifest.json, pyproject.toml, src/opencomputeruseagent/__init__.py and
the README's current version line. The wrapper's UPSTREAM_VERSION, UPSTREAM_COMMIT and
PATCH_REVISION must equal vendor/open-computer-use/pin.json.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src/opencomputeruseagent/__init__.py"
README_LINE = re.compile(r"^Current version: \*\*(\d+\.\d+\.\d+)\*\*", re.M)


def read():
    init = INIT.read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    values = {
        "manifest.json": json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["version"],
        "pyproject.toml": re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
        "__init__.py": re.search(r'^__version__ = "([^"]+)"', init, re.M).group(1),
        "README.md": (README_LINE.search(readme) or [None, None])[1],
    }
    pin = json.loads((ROOT / "vendor/open-computer-use/pin.json").read_text(encoding="utf-8"))
    engine = {key: re.search(rf'^{key} = "([^"]+)"', init, re.M).group(1)
              for key in ("UPSTREAM_VERSION", "UPSTREAM_COMMIT", "PATCH_REVISION")}
    expected = {"UPSTREAM_VERSION": pin["upstream_version"], "UPSTREAM_COMMIT": pin["upstream_commit"],
                "PATCH_REVISION": pin["patch_revision"]}
    return values, engine, expected


def check():
    values, engine, expected = read()
    problems = []
    if len(set(values.values())) != 1:
        problems.append("version differs: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    for key, value in expected.items():
        if engine[key] != value:
            problems.append(f"{key} is {engine[key]} in __init__.py but {value} in pin.json")
    return problems


def bump(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("version must be X.Y.Z")
    path = ROOT / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version"] = version
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for path, pattern, replacement in (
        (ROOT / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"'),
        (INIT, r'^__version__ = "[^"]+"', f'__version__ = "{version}"'),
        (ROOT / "README.md", r"^Current version: \*\*\d+\.\d+\.\d+\*\*", f"Current version: **{version}**"),
    ):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(pattern, replacement, text, count=1, flags=re.M), encoding="utf-8")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "bump":
        bump(sys.argv[2])
    elif len(sys.argv) != 2 or sys.argv[1] != "check":
        raise SystemExit(__doc__)
    problems = check()
    if problems:
        raise SystemExit("\n".join(problems))
    print("versions agree:", read()[0]["manifest.json"])


if __name__ == "__main__":
    main()
