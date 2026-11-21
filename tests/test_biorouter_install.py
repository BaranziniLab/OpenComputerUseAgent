#!/usr/bin/env python3
"""L6: the bundle installed and driven through Biorouter's own MCP client.

    python3 tests/test_biorouter_install.py --bin <folder with biorouter and biorouterd> \
        --bundle dist/opencomputeruseagent.brxt --root /tmp/ocua-l6 [--answer allow --app desktop-fixture]

Everything runs in an isolated profile (`BIOROUTER_PATH_ROOT` and `HOME` under --root,
`BIOROUTER_DISABLE_KEYRING=true`); nothing touches the real profile or the credential store.

1. `biorouter extension install <bundle>`, then `biorouter extension list` shows it.
2. `biorouterd agent` starts with a secret and a user action key made for this run (its SHA-256
   on stdin, as the desktop does). The key lives only in this process's memory.
3. `POST /agent/start`, then `GET /agent/tools` lists the twelve `opencomputeruseagent__` tools.
4. Real turns over `/reply` with a scripted OpenAI compatible model (never `/agent/call_tool`,
   where a card cannot surface): the card appears in the session's stream, an answer without
   `X-User-Action` gets 403, and the proven answer reaches the extension.
5. A protected app is refused without a card.
6. `biorouter extension remove --purge` deletes the files and leaves no helper running.

`--answer deny` (the default) never starts the helper, so it is safe on a Mac whose helper holds
no permission. `--answer allow` with `--app` drives the helper (Linux in Docker, or a granted Mac).
"""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "opencomputeruseagent"
DENY, ALLOW = "Don't allow", "Allow looking and acting"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root).resolve()
        self.secret = secrets.token_hex(16)
        self.user_key = secrets.token_hex(32)
        self.port, self.model_port = free_port(), free_port()
        self.procs = []
        self.results = {"checks": {}}

    def check(self, name, ok, detail=None):
        self.results["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        if not ok:
            print(f"    detail: {str(detail)[:800]}", flush=True)
        return ok

    def env(self):
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        base = {"HOME": str(home), "USER": os.environ.get("USER", "tester"), "LOGNAME": os.environ.get("USER", "tester"),
                "LANG": "en_US.UTF-8", "PATH": os.pathsep.join([str(Path(shutil.which("uv")).parent), "/usr/bin", "/bin",
                                                                 "/usr/sbin", "/sbin"]),
                "BIOROUTER_PATH_ROOT": str(self.root / "br-root"), "BIOROUTER_DISABLE_KEYRING": "true",
                "BIOROUTER_PORT": str(self.port), "BIOROUTER_SERVER__SECRET_KEY": self.secret,
                "BIOROUTER_PROVIDER": "openai", "BIOROUTER_MODEL": "gpt-4o",
                "OPENAI_HOST": f"http://127.0.0.1:{self.model_port}", "OPENAI_API_KEY": "scripted-not-a-key",
                "BIOROUTER_MODE": self.args.mode, "RUST_LOG": "info"}
        for key in ("DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XAUTHORITY", "TMPDIR"):
            if os.environ.get(key):
                base[key] = os.environ[key]
        return base

    def cli(self, *arguments, timeout=600):
        return subprocess.run([str(Path(self.args.bin) / "biorouter"), *arguments], capture_output=True, text=True,
                              env=self.env(), timeout=timeout, cwd=str(self.root))

    def http(self, method, path, body=None, proof=True, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        request.add_header("X-Secret-Key", self.secret)
        request.add_header("Content-Type", "application/json")
        if proof:
            request.add_header("X-User-Action", self.user_key)
        def parse(raw):
            try:
                return json.loads(raw or b"null")
            except ValueError:
                return raw.decode(errors="replace")[:500]

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, parse(response.read())
        except urllib.error.HTTPError as error:
            return error.code, parse(error.read())

    def turn(self, session, text, on_card):
        message = {"role": "user", "created": int(time.time()), "content": [{"type": "text", "text": text}],
                   "metadata": {"userVisible": True, "agentVisible": True}}
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/reply",
                                         data=json.dumps({"user_message": message, "session_id": session}).encode(),
                                         method="POST")
        for key, value in (("X-Secret-Key", self.secret), ("Content-Type", "application/json"),
                           ("X-User-Action", self.user_key)):
            request.add_header(key, value)
        cards, results = [], []
        with urllib.request.urlopen(request, timeout=600) as response:
            for raw in response:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                if event.get("type") == "Message":
                    for content in event["message"].get("content", []):
                        data = content.get("data", {}) if content.get("type") == "actionRequired" else {}
                        if data.get("actionType") == "elicitation":
                            cards.append(data)
                            threading.Thread(target=on_card, args=(session, data), daemon=True).start()
                        if content.get("type") == "toolResponse":
                            results.append(json.dumps(content.get("toolResult", {})))
                if event.get("type") in ("Finish", "Error"):
                    break
        return cards, results

    def answer(self, session, card, value, proof=True):
        return self.http("POST", "/action-required/elicitation",
                         {"session_id": session, "id": card["id"], "data": {"access": value}}, proof=proof)

    def helpers(self):
        rows = subprocess.run(["ps", "-axo", "pid=,command="] if sys.platform == "darwin" else ["ps", "-eo", "pid=,args="],
                              capture_output=True, text=True).stdout.splitlines()
        install = str(self.root / "br-root")
        return [r.strip() for r in rows if install in r and ("/ocu" in r or "Open Computer Use Agent" in r)]

    def main(self):
        bundle = Path(self.args.bundle).resolve()
        installed = self.cli("extension", "install", str(bundle))
        self.check("install", installed.returncode == 0, (installed.stdout + installed.stderr)[-800:])
        listed = self.cli("extension", "list")
        self.check("listed and enabled", NAME in listed.stdout, listed.stdout[-800:])
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        self.procs.append(subprocess.Popen([sys.executable, str(ROOT / "tests/fake_openai.py"), str(self.model_port),
                                            str(logs / "model.jsonl")]))
        daemon = subprocess.Popen([str(Path(self.args.bin) / "biorouterd"), "agent"], stdin=subprocess.PIPE,
                                  stdout=open(logs / "daemon.out", "w"), stderr=subprocess.STDOUT, env=self.env(),
                                  cwd=str(self.root), start_new_session=True)
        self.procs.append(daemon)
        daemon.stdin.write((hashlib.sha256(self.user_key.encode()).hexdigest() + "\n").encode())
        daemon.stdin.close()
        for _ in range(300):
            try:
                if self.http("GET", "/status", proof=False, timeout=2)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        status, started = self.http("POST", "/agent/start", {"working_dir": str(work)})
        if not self.check("session started", status == 200, started):
            return
        session = started["id"]
        status, tools = self.http("GET", f"/agent/tools?session_id={session}&extension_name={NAME}", timeout=120)
        names = sorted(t.get("name") for t in tools) if isinstance(tools, list) else tools
        self.check("twelve tools through Biorouter", isinstance(names, list) and len(names) == 12
                   and all(n.startswith(NAME + "__") for n in names), names)

        answers = {}

        def on_card(sess, card):
            time.sleep(0.5)
            answers["unproven"] = self.answer(sess, card, ALLOW, proof=False)
            answers["proven"] = self.answer(sess, card, self.args.answer_value)
            answers["second"] = self.answer(sess, card, ALLOW)

        app = self.args.app
        cards, results = self.turn(session, f'CALL {NAME}__get_app_state {{"app": "{app}"}}', on_card)
        time.sleep(1)
        self.check("one card in the chat's stream", len(cards) == 1, cards)
        if cards:
            schema = cards[0].get("requested_schema") or cards[0].get("requestedSchema") or {}
            self.check("card offers the three answers",
                       (schema.get("properties", {}).get("access", {}).get("enum") == [DENY, "Only allow looking", ALLOW]),
                       schema)
        self.check("answer without the user action proof is refused", answers.get("unproven", [0])[0] == 403,
                   answers.get("unproven"))
        self.check("proven answer accepted", answers.get("proven", [0])[0] == 200, answers.get("proven"))
        self.check("second answer refused", answers.get("second", [0])[0] in (404, 409), answers.get("second"))
        joined = " ".join(results)
        if self.args.answer_value == DENY:
            self.check("declined card refuses the call", "did not allow" in joined, joined[:600])
        else:
            self.check("allowed call returns the app state", f"App={app}" in joined or "App=" in joined, joined[:600])

        # Biorouter delivers any answer as accept, checking neither its keys nor its value against
        # the card's schema: the extension must refuse one that is not exactly an offered label.
        def odd(sess, card):
            time.sleep(0.5)
            answers["odd"] = self.http("POST", "/action-required/elicitation",
                                       {"session_id": sess, "id": card["id"],
                                        "data": {"access": "rm -rf everything", "extra": 1}})

        cards, results = self.turn(session, f'CALL {NAME}__get_app_state {{"app": "{app}"}}', odd)
        self.check("an answer outside the offered labels is delivered by Biorouter",
                   len(cards) == 1 and answers.get("odd", [0])[0] == 200, answers.get("odd"))
        self.check("and refused by the extension", "did not allow" in " ".join(results), " ".join(results)[:400])

        # A batch: Biorouter sends the calls of one model reply at once. One action asks; the other,
        # sent while the first is still in flight, is refused at once and never waits for a card.
        def deny(sess, card):
            time.sleep(0.5)
            answers["batch"] = self.answer(sess, card, DENY)

        batch = json.dumps([[f"{NAME}__click", {"app": app, "element_index": "0"}],
                            [f"{NAME}__type_text", {"app": app, "text": "x"}]])
        cards, results = self.turn(session, f"CALLS {batch}", deny)
        joined = " ".join(results)
        self.check("a batch of two actions raises one card and refuses the second action",
                   len(cards) == 1 and len(results) == 2 and "did not allow" in joined
                   and "Another action in this chat was still running" in joined, joined[:800])

        cards, results = self.turn(session, f'CALL {NAME}__get_app_state {{"app": "Biorouter"}}', lambda s, c: None)
        self.check("Biorouter's own app refused without a card", not cards and "Biorouter itself" in " ".join(results),
                   " ".join(results)[:400])
        cards, results = self.turn(session, f'CALL {NAME}__press_key {{"app": "{app}", "key": "cmd+v"}}',
                                   lambda s, c: None)
        self.check("paste chord refused without a card", not cards and "Copy, cut and paste" in " ".join(results),
                   " ".join(results)[:400])

    def finish(self):
        for process in reversed(self.procs):
            try:
                if process.args[-1] == "agent":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except OSError:
                pass
        for process in self.procs:
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()
        time.sleep(2)
        removed = self.cli("extension", "remove", NAME, "--purge")
        install_dirs = list((self.root / "br-root").rglob(f"extensions/{NAME}"))
        self.check("remove --purge", removed.returncode == 0 and not [d for d in install_dirs if d.exists()],
                   (removed.stdout + removed.stderr)[-400:])
        self.check("no helper left running", not self.helpers(), self.helpers())
        self.results["passed"] = all(c["ok"] for c in self.results["checks"].values())
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "results.json").write_text(json.dumps(self.results, indent=2, default=str))
        return 0 if self.results["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bin", required=True, help="folder holding biorouter and biorouterd")
    parser.add_argument("--bundle", default=str(ROOT / "dist/opencomputeruseagent.brxt"))
    parser.add_argument("--root", required=True, help="an empty folder for the isolated profile")
    parser.add_argument("--answer", choices=("deny", "allow"), default="deny")
    parser.add_argument("--app", default="TextEdit")
    parser.add_argument("--mode", default="auto", help="Biorouter permission mode (auto, smart_approve, approve)")
    args = parser.parse_args()
    args.answer_value = DENY if args.answer == "deny" else ALLOW
    run = Run(args)
    try:
        run.main()
    except Exception as error:
        run.check("harness ran", False, repr(error))
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
