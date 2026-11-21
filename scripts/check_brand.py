#!/usr/bin/env python3
"""Brand checks for the patched helper source and for this repository's own text.

    python3 scripts/check_brand.py --patched build/source.noindex   # the staged, patched tree
    python3 scripts/check_brand.py --repo                           # this repository

The helper must never call itself Biorouter Copilot, never carry Biorouter's bundle
identifiers, and never ask the person to grant a permission to Biorouter: every permission
belongs to the helper app. This repository's own text spells the product "Biorouter".
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Anything naming Biorouter in the patched helper is a leftover from the earlier vendoring:
# the helper is a separate product and names only itself.
PATCHED_FORBIDDEN = [
    (re.compile(r"copilot", re.I), "names Copilot"),
    (re.compile(r"biorouter", re.I), "names Biorouter"),
    (re.compile(r"org\.biorouter|edu\.ucsf\.biorouter|com\.electron\.biorouter", re.I), "carries a Biorouter bundle identifier"),
]
GRANT_TO_BIOROUTER = re.compile(r"\b(grant|allow|enable)\b[^.\n]{0,80}\bbiorouter\b", re.I)

# Our own text: the product is "Biorouter", never "BioRouter".
REPO_FORBIDDEN = [
    (re.compile(r"BioRouter"), "spells Biorouter as BioRouter"),
]
# One exception: Biorouter's core names the shared desktop lock's folder "BioRouter", and the
# fallback must use that exact path to share the lock with Biorouter Copilot.
LOCK_PATH = re.compile(r"BioRouter[\"']?\s*/\s*[\"']?computer-use\.desktop\.lock")
REPO_SKIP = ("vendor/open-computer-use/source/", "vendor/open-computer-use/patches/", "build", ".git/", ".pytest_cache/",
             ".venv/", "dist/", "scripts/check_brand.py")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".sh", ".swift", ".cs", ".txt", ""}


def text_files(root, skip=()):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or "/.git/" in relative or any(relative.startswith(s) for s in skip):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield relative, text


def check_tree(root):
    problems = []
    for relative, text in text_files(Path(root)):
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, why in PATCHED_FORBIDDEN:
                if pattern.search(line):
                    problems.append(f"{relative}:{number} {why}: {line.strip()[:120]}")
            if GRANT_TO_BIOROUTER.search(line):
                problems.append(f"{relative}:{number} asks for a grant to Biorouter: {line.strip()[:120]}")
    return problems


def check_repo(root=ROOT):
    problems = []
    for relative, text in text_files(Path(root), REPO_SKIP):
        if Path(relative).suffix not in TEXT_SUFFIXES:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            line = LOCK_PATH.sub("", line)
            for pattern, why in REPO_FORBIDDEN:
                if pattern.search(line):
                    problems.append(f"{relative}:{number} {why}: {line.strip()[:120]}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patched", type=Path, help="a staged, patched helper source tree")
    parser.add_argument("--repo", action="store_true", help="check this repository's own text")
    args = parser.parse_args()
    problems = []
    if args.patched:
        problems += check_tree(args.patched)
    if args.repo:
        problems += check_repo()
    if not args.patched and not args.repo:
        parser.error("pass --patched <dir>, --repo, or both")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        raise SystemExit(f"{len(problems)} brand problem(s)")
    print("brand check passed")


if __name__ == "__main__":
    main()
