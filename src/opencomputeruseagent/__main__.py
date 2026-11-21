"""Entry point: `uv run --directory <install folder> opencomputeruseagent.py`."""
import sys


def main():
    if sys.version_info < (3, 9):
        sys.stderr.write("Open Computer Use Agent needs Python 3.9 or later.\n")
        raise SystemExit(1)
    from .server import run_stdio

    raise SystemExit(run_stdio())


if __name__ == "__main__":
    main()
