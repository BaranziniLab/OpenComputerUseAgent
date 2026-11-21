"""A stand-in for `ocu mcp` used by the unit tests: MCP over stdio, behaviour set by FAKE_MODE.

Modes: normal, wrong_version, wrong_schema, slow (a call sleeps), stuck (a call blocks and the
helper stops reading, so a cancel cannot settle), honor_cancel (a cancelled call ends quietly).
FAKE_SLOW_TOOLS limits slow and honor_cancel to the tools it names.
FAKE_LOG, when set, receives one JSON line per message the helper read and its environment.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "src/opencomputeruseagent/contract/tools.json").read_text())
MODE = os.environ.get("FAKE_MODE", "normal")
LOG = os.environ.get("FAKE_LOG")
lock = threading.Lock()
cancelled = set()


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def send(message):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


STATE = 'App={app} (pid {pid})\nWindow: "{title}", App: {name}.\n\t0 window {title}\n\nThe focused UI element is text Field.'


def result_for(name, arguments):
    app = arguments.get("app", "")
    pid = int(os.environ.get("FAKE_PID", "4242"))
    if os.environ.get("FAKE_STATE_APP") and app:
        app_ref = os.environ["FAKE_STATE_APP"]
    else:
        app_ref = app
    if name == "list_apps":
        lines = os.environ.get("FAKE_LIST", "Editor -- Editor [running, pid=4242, window=Doc]").split("|")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    if name == "screen_capture":
        if arguments.get("list_only"):
            target = json.loads(os.environ.get("FAKE_CAPTURE_TARGET", '{"title": "Doc", "app": "Editor", "pid": 4242}'))
            return {"content": [{"type": "text", "text": json.dumps({"windows": [target], "target": target})}]}
        return {"content": [{"type": "text", "text": "{}"}, {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]}
    if os.environ.get("FAKE_FAIL") == name:
        return {"content": [{"type": "text", "text": f"{name} failed"}], "isError": True}
    text = STATE.format(app=app_ref, pid=pid, title=os.environ.get("FAKE_TITLE", "Doc"), name=app or "Editor")
    return {"content": [{"type": "text", "text": text}, {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]}


def is_slow(name):
    """FAKE_SLOW_TOOLS (a comma list) limits slow and honor_cancel to those tools."""
    listed = [n for n in os.environ.get("FAKE_SLOW_TOOLS", "").split(",") if n]
    return MODE in ("slow", "honor_cancel") and (not listed or name in listed)


def run_call(message):
    params = message.get("params") or {}
    name, arguments = params.get("name"), params.get("arguments") or {}
    if is_slow(name):
        deadline = time.time() + float(os.environ.get("FAKE_DELAY", "5"))
        while time.time() < deadline:
            if message["id"] in cancelled:
                return
            time.sleep(0.02)
    send({"jsonrpc": "2.0", "id": message["id"], "result": result_for(name, arguments)})


def main():
    log({"env": dict(os.environ), "argv": sys.argv[1:]})
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        log({"message": message})
        method = message.get("method")
        if method == "initialize":
            version = "9.9.9" if MODE == "wrong_version" else "1.1.5"
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "open-computer-use", "version": version},
                "instructions": "Open Computer Use Agent is a fallback for desktop control."}})
        elif method == "tools/list":
            listed = [dict(name=t["name"], description="x", inputSchema=t["inputSchema"]) for t in CONTRACT["tools"]]
            if MODE == "wrong_schema":
                listed[0]["inputSchema"] = {"type": "object", "properties": {}}
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": listed}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        elif method == "tools/call":
            if MODE == "stuck":
                time.sleep(600)
            if MODE in ("slow", "honor_cancel"):
                threading.Thread(target=run_call, args=(message,), daemon=True).start()
            else:
                run_call(message)
        elif method == "notifications/cancelled":
            cancelled.add((message.get("params") or {}).get("requestId"))


if __name__ == "__main__":
    main()
