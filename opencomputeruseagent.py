"""Launcher Biorouter runs as the extension's entry point.

The project is not an installable package (`[tool.uv] package = false`), so `uv sync` needs no
build backend and no network. This script puts `src/` on the import path and starts the server.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from opencomputeruseagent.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
