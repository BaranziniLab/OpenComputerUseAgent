"""L2: the helper alone over stdio.

    OCUA_PAYLOAD_DIR=build/payload/darwin-arm64 uv run --group test pytest tests/test_helper_protocol.py
    python3 tests/test_helper_protocol.py build/payload/linux-x64

`initialize` answers protocol 2025-03-26 with the pinned version, exactly the ten tools are
listed with the pinned schemas, and the instructions name Open Computer Use Agent and never
Biorouter Copilot. On macOS this alone sets OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY=1, so the
helper runs in this process and no permission is touched (no tool is called).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opencomputeruseagent import UPSTREAM_VERSION, tools  # noqa: E402


def exchange(executable, environment):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    completed = subprocess.run([str(executable), "mcp"], input=stdin.encode(), capture_output=True,
                               env=environment, timeout=60)
    answers = {}
    for line in completed.stdout.decode().splitlines():
        if line.strip():
            message = json.loads(line)
            answers[message.get("id")] = message
    return answers, completed


def check_payload(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    executable = directory / manifest["executable"]
    private = tempfile.mkdtemp(prefix="ocua-l2-")
    environment = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", private), "TMPDIR": private,
                   "OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE": "ocua-test-l2",
                   "OPEN_COMPUTER_USE_CONFIG_FILE": os.path.join(private, "none.json")}
    if manifest["target"].startswith("darwin-"):
        environment["OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY"] = "1"
    if manifest["target"].startswith("win32-"):
        environment = dict(os.environ, **{k: v for k, v in environment.items() if k != "PATH"})
    answers, completed = exchange(executable, environment)
    assert 1 in answers and 2 in answers, completed.stderr.decode()[-2000:]
    init = answers[1]["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["version"] == UPSTREAM_VERSION
    instructions = init.get("instructions") or ""
    assert "Open Computer Use Agent" in instructions
    assert "copilot" not in instructions.lower() and "biorouter" not in instructions.lower()
    listed = answers[2]["result"]["tools"]
    assert sorted(t["name"] for t in listed) == sorted(tools.HELPER_TOOLS)
    assert tools.schema_mismatches(listed) == []
    for tool in listed:
        text = json.dumps(tool).lower()
        assert "copilot" not in text and "biorouter" not in text, tool["name"]
    assert answers[3]["result"] == {}
    return {"target": manifest["target"], "tools": len(listed), "version": init["serverInfo"]["version"]}


def _payload_dirs():
    raw = os.environ.get("OCUA_PAYLOAD_DIR", "")
    return [p for p in raw.split(os.pathsep) if p]


@pytest.mark.skipif(not _payload_dirs(), reason="set OCUA_PAYLOAD_DIR to a built payload folder")
@pytest.mark.parametrize("directory", _payload_dirs() or ["unset"])
def test_helper_protocol(directory):
    check_payload(directory)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(json.dumps(check_payload(path)))
