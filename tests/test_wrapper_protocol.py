#!/usr/bin/env python3
"""L2: the wrapper over stdio with the real helper payload and a scripted client.

    OCUA_PAYLOAD_DIR=build/payload/darwin-arm64 uv run --group test pytest tests/test_wrapper_protocol.py
    python3 tests/test_wrapper_protocol.py build/payload/linux-x64

Twelve tools with the designed annotations, a card before the first look, a refusal when the
client declares no elicitation capability, and the declined card refusing the call. The card is
always declined here, so the helper never starts and no permission is touched on macOS; the
fixture drives (tests/test_linux_fixture.py, tests/test_windows_fixture.py and
scripts/macos_live_drive.py) cover allowed calls.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    import pytest
except ImportError:  # run as a plain script where pytest is not installed (a distro image)
    class _Mark:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: (lambda function: function)

    class pytest:  # noqa: N801
        mark = _Mark()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from mcp_client import Client, make_install  # noqa: E402
from opencomputeruseagent import consent, tools  # noqa: E402
from opencomputeruseagent.payload import target as detect_target  # noqa: E402


def check_wrapper(payload_dir):
    payload_dir = Path(payload_dir)
    manifest = json.loads((payload_dir / "manifest.json").read_text())
    if manifest["target"] != detect_target():
        return {"skipped": f"{manifest['target']} does not run on this computer"}
    install = make_install(Path(tempfile.mkdtemp(prefix="ocua-l2w-")) / "install", {manifest["target"]: payload_dir})
    env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="ocua-home-"))
    if manifest["target"].startswith("linux-"):
        # The wrapper refuses before asking when no desktop session is named (a CI runner or a
        # plain container has none). Every card here is declined, so the helper never starts and
        # these placeholders are never used; a real session's values are kept.
        for name, value in (("XDG_RUNTIME_DIR", tempfile.mkdtemp(prefix="ocua-run-")),
                            ("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/ocua-l2w-bus"), ("DISPLAY", ":99")):
            env.setdefault(name, value)
    client = Client(install, env, answer=lambda params: {"action": "accept", "content": {"access": consent.DENY}})
    try:
        listed = client.request("tools/list", {})["tools"]
        assert [t["name"] for t in listed] == list(tools.TABLE)
        for tool in listed:
            assert tool["annotations"] == tools.advertised()[[t["name"] for t in tools.advertised()].index(tool["name"])]["annotations"]
        refused = client.call("get_app_state", {"app": "TextEdit"})
        assert refused["isError"] and "did not allow" in client.text(refused), client.text(refused)
        assert len(client.cards) == 1
        assert len(client.cards[0]["message"]) < 100
    finally:
        client.close()
    blind = Client(install, env, elicitation=False)
    try:
        refused = blind.call("get_app_state", {"app": "TextEdit"})
        assert refused["isError"] and "cannot show the question" in blind.text(refused) and blind.cards == [], \
            blind.text(refused)
    finally:
        blind.close()
    return {"target": manifest["target"], "tools": len(listed)}


def _payload_dirs():
    return [p for p in os.environ.get("OCUA_PAYLOAD_DIR", "").split(os.pathsep) if p]


@pytest.mark.skipif(not _payload_dirs(), reason="set OCUA_PAYLOAD_DIR to a built payload folder")
@pytest.mark.parametrize("directory", _payload_dirs() or ["unset"])
def test_wrapper_protocol(directory):
    check_wrapper(directory)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(json.dumps(check_wrapper(path)))
