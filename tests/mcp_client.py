"""A scripted MCP client for the wrapper's stdio protocol tests (and the Linux fixture drive).

It starts the wrapper the way Biorouter does (the launcher script in an install folder), puts
`_meta.biorouter-session-id` on every call as Biorouter's `inject_session_id_into_extensions`
does, and answers `elicitation/create` with a callback standing in for the person.
"""
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_install(destination, payload_dirs=None):
    """Lay out an install folder as a .brxt extraction does: src/, the launcher, payload/."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("opencomputeruseagent.py", "manifest.json", "pyproject.toml"):
        shutil.copy2(ROOT / name, destination / name)
    sys.path.insert(0, str(ROOT / "src"))
    from opencomputeruseagent import __version__

    for target, directory in (payload_dirs or {}).items():
        shutil.copytree(directory, destination / "payload" / target / __version__)
    return destination


class Client:
    def __init__(self, install, env=None, answer=None, elicitation=True, command=None):
        self.answer = answer or (lambda params: {"action": "decline"})
        self.cards = []
        self._ids = itertools.count(1)
        self._responses = {}
        self._cond = threading.Condition()
        command = command or [sys.executable, str(Path(install) / "opencomputeruseagent.py")]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env if env is not None else dict(os.environ),
                                        cwd=str(install))
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        capabilities = {"sampling": {}}
        if elicitation:
            capabilities["elicitation"] = {}
        self.initialize = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": capabilities,
                                                      "clientInfo": {"name": "scripted-client", "version": "0"}})
        self.notify("notifications/initialized", {})

    def _write(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def _read(self):
        for raw in iter(self.process.stdout.readline, b""):
            message = json.loads(raw.decode())
            if message.get("method") == "elicitation/create":
                self.cards.append(message["params"])
                threading.Thread(target=self._answer, args=(message,), daemon=True).start()
                continue
            with self._cond:
                self._responses[message.get("id")] = message
                self._cond.notify_all()

    def _answer(self, message):
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": self.answer(message["params"])})

    def _read_stderr(self):
        for raw in iter(self.process.stderr.readline, b""):
            self.stderr.append(raw.decode(errors="replace").rstrip())

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def send(self, method, params):
        request_id = next(self._ids)
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def wait(self, request_id, timeout=90):
        with self._cond:
            if not self._cond.wait_for(lambda: request_id in self._responses, timeout):
                raise TimeoutError(f"no answer to {request_id}; stderr: {self.stderr[-10:]}")
            return self._responses.pop(request_id)

    def answered(self, request_id):
        with self._cond:
            return request_id in self._responses

    def request(self, method, params, timeout=90):
        message = self.wait(self.send(method, params), timeout)
        if "error" in message:
            raise RuntimeError(message["error"])
        return message["result"]

    def call(self, name, arguments=None, session="chat-1", timeout=90):
        params = {"name": name, "arguments": arguments or {}}
        if session is not None:
            params["_meta"] = {"biorouter-session-id": session, "progressToken": 1}
        return self.request("tools/call", params, timeout)

    def text(self, result):
        return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
