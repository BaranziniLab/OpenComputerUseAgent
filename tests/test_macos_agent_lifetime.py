#!/usr/bin/env python3
"""The macOS app agent's lifetime (patch 0009), measured without any permission grant.

    python3 tests/test_macos_agent_lifetime.py build/payload/darwin-arm64
    OCUA_PAYLOAD_DIR=build/payload/darwin-arm64 uv run --group test pytest tests/test_macos_agent_lifetime.py

On macOS the helper's `ocu` is only a proxy: LaunchServices starts "Open Computer Use Agent.app"
as its own process, which holds the grants. Upstream's agent never exited, and a SIGTERM (what
the wrapper sends a stale agent) left its socket file behind. This drive installs the payload
the way a .brxt install writes it, runs the grant free `doctor --json` with the wrapper's own
scrubbed environment, and checks that:

1. the agent runs as its own process (parent launchd), from this install's app;
2. with no connection it exits on its own (the test shortens the idle time with
   OPEN_COMPUTER_USE_APP_AGENT_IDLE_EXIT_SECONDS; the wrapper never passes it), and removes its
   socket;
3. a SIGTERM through the wrapper's own `end_app_agents` ends it and removes its socket too.

Nothing here asks for Accessibility or Screen Recording, opens a window or touches a setting:
`doctor --json` only reads the permission state.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import pytest
except ImportError:  # run as a plain script
    class _Mark:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: (lambda function: function)

    class pytest:  # noqa: N801
        mark = _Mark()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opencomputeruseagent import APP_NAME, __version__, procs  # noqa: E402
from opencomputeruseagent.dispatch import end_app_agents  # noqa: E402
from opencomputeruseagent.payload import Payload, helper_environment  # noqa: E402

IDLE_SECONDS = 6


def user_temp_dir():
    return Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())


def socket_path(namespace):
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:16]
    return user_temp_dir() / f"open-computer-use-agent-{digest}.sock"


def agents(app_executable):
    """Agents of this app: parent launchd, executable this app's (LaunchServices reports the
    resolved path, so the comparison is between real paths)."""
    real = os.path.realpath(str(app_executable))
    return [row.pid for row in procs.table()
            if row.ppid == 1 and row.exe.endswith("/ocu") and os.path.realpath(row.exe) == real]


def wait_for(predicate, timeout, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def install_like_biorouter(payload_dir):
    """Plain file copies with no modes, as `BrxtBundle::extract_to` writes them."""
    # Resolved, as the wrapper's own install root is (`/var/...` is `/private/var/...`).
    root = Path(tempfile.mkdtemp(prefix="ocua-lifetime-")).resolve() / "opencomputeruseagent"
    destination = root / "payload" / json.loads((payload_dir / "manifest.json").read_text())["target"] / __version__
    for source in sorted(p for p in payload_dir.rglob("*") if p.is_file()):
        target = destination / source.relative_to(payload_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as reader, open(target, "wb") as writer:
            shutil.copyfileobj(reader, writer)
    return root


def doctor(payload, idle=None):
    private = Path(tempfile.mkdtemp(prefix="ocua-lifetime-tmp-"))
    env = helper_environment(payload.target, os.environ, private, payload.namespace())
    if idle is not None:
        env["OPEN_COMPUTER_USE_APP_AGENT_IDLE_EXIT_SECONDS"] = str(idle)
    completed = subprocess.run([str(payload.executable), "doctor", "--json"], capture_output=True, text=True,
                               env=env, cwd=str(private), timeout=60)
    shutil.rmtree(private, ignore_errors=True)
    return completed


def check_lifetime(payload_dir):
    payload_dir = Path(payload_dir)
    manifest = json.loads((payload_dir / "manifest.json").read_text())
    if sys.platform != "darwin" or manifest["target"] != ("darwin-arm64" if os.uname().machine == "arm64" else "darwin-x64"):
        return {"skipped": f"{manifest['target']} does not run on this computer"}
    root = install_like_biorouter(payload_dir)
    payload = Payload(manifest["target"], root)
    payload.verify()  # restores 0755 on the executable, as the wrapper does
    app_executable = payload.directory / f"{APP_NAME}.app" / "Contents" / "MacOS" / "ocu"
    sock = socket_path(payload.namespace())
    report = {"target": manifest["target"], "socket": str(sock)}
    try:
        began = time.monotonic()
        answer = doctor(payload, idle=IDLE_SECONDS)
        assert answer.returncode == 0, answer.stderr
        status = json.loads(answer.stdout.strip().splitlines()[-1])
        report["doctor"] = {k: status.get(k) for k in ("state", "accessibility", "screen_recording")}
        assert "Biorouter" not in status.get("message", ""), status
        running = agents(app_executable)
        assert len(running) == 1, f"expected one agent with parent launchd, found {running}"
        assert sock.exists(), "the agent's socket is missing"
        assert wait_for(lambda: not agents(app_executable), IDLE_SECONDS + 30), "the idle agent did not exit"
        report["idle_exit_seconds"] = round(time.monotonic() - began, 1)
        assert wait_for(lambda: not sock.exists(), 5), "the idle exit left its socket behind"

        answer = doctor(payload)  # the ten minute default: only the SIGTERM ends this one
        assert answer.returncode == 0, answer.stderr
        running = agents(app_executable)
        assert len(running) == 1, running
        ended = end_app_agents([payload.directory])
        assert ended == running, (ended, running)
        assert wait_for(lambda: not agents(app_executable), 10), "SIGTERM did not end the agent"
        assert wait_for(lambda: not sock.exists(), 5), "SIGTERM left the socket behind"
        report["sigterm"] = "ended, socket removed"
        return report
    finally:
        for pid in agents(app_executable):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        shutil.rmtree(root.parent, ignore_errors=True)


def _payload_dirs():
    return [p for p in os.environ.get("OCUA_PAYLOAD_DIR", "").split(os.pathsep) if p]


@pytest.mark.skipif(not _payload_dirs() or sys.platform != "darwin", reason="set OCUA_PAYLOAD_DIR on macOS")
@pytest.mark.parametrize("directory", _payload_dirs() or ["unset"])
def test_agent_lifetime(directory):
    check_lifetime(directory)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(json.dumps(check_lifetime(path)))
