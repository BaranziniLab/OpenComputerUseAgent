#!/usr/bin/env python3
"""L6, terminal: the bundle installed into Biorouter's CLI and driven at a real terminal (a pty).

    python3 tests/test_biorouter_cli.py --bin <folder with biorouter> --root /tmp/ocua-l6-cli \
        [--bundle dist/opencomputeruseagent.brxt]

Biorouter's default terminal interface (the full screen TUI) cannot show an MCP elicitation: in
1.94.6 it cancels the whole turn when one arrives. The extension therefore notices that
interface and refuses without a card, so the turn goes on and the model can say why. The
classic interface (`BIOROUTER_CLI_CLASSIC=1`) shows the card as a prompt, and passes any text
typed there straight through as the answer, which the extension must then check itself.

Each case runs `biorouter run --no-session -s -t 'CALL ...'` in a pty against a scripted model
(tests/fake_openai.py). The oracle is the scripted model's own log: what tool result the model
was handed, which proves the turn reached the model rather than being cancelled. Every card is
refused here, so the helper never starts and no permission is touched. Everything runs in an
isolated profile (`BIOROUTER_PATH_ROOT` and `HOME` under --root, `BIOROUTER_DISABLE_KEYRING=true`).
"""
import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "opencomputeruseagent"
ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>78]|\x1b\][^\x07]*\x07")
# The classic prompt: `access (<description>)* [<default>]: ` (the default part when the field has one).
PROMPT = re.compile(r"(^|\n)access \(.*\)\*( \[[^\]]*\])?:\s*$", re.S)
TUI_CANCEL = "not yet supported in the TUI"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean(raw):
    return ANSI.sub(b"", raw).decode("utf-8", "replace").replace("\r", "")


class Run:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root).resolve()
        self.model_port = free_port()
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.model_log = self.logs / "model.jsonl"
        self.results = {"checks": {}}
        self.model = None

    def check(self, name, ok, detail=None):
        self.results["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        return ok

    def env(self, classic):
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"HOME": str(home), "USER": os.environ.get("USER", "tester"), "LOGNAME": os.environ.get("USER", "tester"),
               "LANG": "en_US.UTF-8", "TERM": "xterm-256color",
               "PATH": os.pathsep.join([str(Path(shutil.which("uv")).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
               "BIOROUTER_PATH_ROOT": str(self.root / "br-root"), "BIOROUTER_DISABLE_KEYRING": "true",
               "BIOROUTER_PROVIDER": "openai", "BIOROUTER_MODEL": "gpt-4o",
               "OPENAI_HOST": f"http://127.0.0.1:{self.model_port}", "OPENAI_API_KEY": "scripted-not-a-key",
               "BIOROUTER_MODE": "auto"}
        for key in ("DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XAUTHORITY", "TMPDIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if classic:
            env["BIOROUTER_CLI_CLASSIC"] = "1"
        return env

    def cli(self, *arguments):
        return subprocess.run([str(Path(self.args.bin) / "biorouter"), *arguments], capture_output=True, text=True,
                              env=self.env(False), timeout=600, cwd=str(self.root))

    def model_replies(self):
        if not self.model_log.exists():
            return []
        rows = [json.loads(line) for line in self.model_log.read_text().splitlines() if line.strip()]
        return [row.get("reply") or "" for row in rows if row.get("last_role") == "tool"]

    def at_terminal(self, label, prompt, classic, typed=None, timeout=120):
        """Run one CLI turn in a pty; type `typed` at the card's prompt. Returns the screen text
        and the tool results the scripted model was handed during this turn."""
        before = len(self.model_replies())
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        binary = str(Path(self.args.bin) / "biorouter")
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child
            os.chdir(str(work))
            os.execve(binary, [binary, "run", "--no-session", "-s", "-t", prompt], self.env(classic))
        import fcntl
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))
        raw, answered, leaving, deadline = b"", False, None, time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.25)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    raw += chunk
                text = clean(raw)
                if typed is not None and not answered and PROMPT.search(text.rstrip(" ")):
                    time.sleep(0.3)
                    os.write(fd, (typed + "\n").encode())
                    answered = True
                done = len(self.model_replies()) > before or TUI_CANCEL in text
                if done and leaving is None:
                    leaving = time.monotonic() + 3  # let the reply reach the screen, then leave
                if leaving is not None and time.monotonic() >= leaving:
                    for keys in (b"/exit\r", b"\x04"):
                        try:
                            os.write(fd, keys)
                        except OSError:
                            break
                        time.sleep(0.6)
                    leaving = float("inf")
                    deadline = min(deadline, time.monotonic() + 15)
                finished, _status = os.waitpid(pid, os.WNOHANG)
                if finished == pid:
                    pid = None
                    break
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            os.close(fd)
        text = clean(raw)
        (self.logs / f"{label}.txt").write_text(text)
        return text, self.model_replies()[before:], answered

    def leftover_wrappers(self):
        rows = subprocess.run(["ps", "-axo", "pid=,command="] if sys.platform == "darwin" else ["ps", "-eo", "pid=,args="],
                              capture_output=True, text=True).stdout.splitlines()
        install = str(self.root / "br-root")
        return [r.strip() for r in rows if install in r and ("opencomputeruseagent.py" in r or "/ocu" in r)]

    def main(self):
        bundle = Path(self.args.bundle).resolve()
        installed = self.cli("extension", "install", str(bundle))
        self.check("install", installed.returncode == 0, (installed.stdout + installed.stderr)[-600:])
        self.model = subprocess.Popen([sys.executable, str(ROOT / "tests/fake_openai.py"), str(self.model_port),
                                       str(self.model_log)])
        time.sleep(0.5)
        look = f'CALL {NAME}__get_app_state {{"app": "TextEdit"}}'

        screen, replies, _ = self.at_terminal("tui", look, classic=False)
        joined = " ".join(replies)
        self.check("default terminal interface: the turn is not cancelled", TUI_CANCEL not in screen, screen[-800:])
        self.check("default terminal interface: refused without a card, and the model is told why",
                   "terminal interface cannot show" in joined and "BIOROUTER_CLI_CLASSIC=1" in joined, joined[:600])

        screen, replies, answered = self.at_terminal("classic-deny", look, classic=True, typed="1")
        joined = " ".join(replies)
        self.check("classic interface shows the card with its three answers",
                   answered and "Options: Don't allow, Only allow looking, Allow looking and acting" in screen,
                   screen[-1200:])
        self.check("classic interface: Don't allow refuses", "did not allow" in joined, joined[:600])

        screen, replies, answered = self.at_terminal("classic-free-text", look, classic=True, typed="rm -rf everything")
        joined = " ".join(replies)
        self.check("classic interface: free text typed at the card refuses", answered and "did not allow" in joined,
                   joined[:600])

    def finish(self):
        if self.model is not None:
            self.model.terminate()
            try:
                self.model.wait(10)
            except subprocess.TimeoutExpired:
                self.model.kill()
        time.sleep(1)
        self.check("no wrapper or helper left running", not self.leftover_wrappers(), self.leftover_wrappers())
        removed = self.cli("extension", "remove", NAME, "--purge")
        self.check("remove --purge", removed.returncode == 0, (removed.stdout + removed.stderr)[-300:])
        self.results["passed"] = all(c["ok"] for c in self.results["checks"].values())
        (self.logs / "results.json").write_text(json.dumps(self.results, indent=2, default=str))
        return 0 if self.results["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bin", required=True, help="folder holding biorouter")
    parser.add_argument("--bundle", default=str(ROOT / "dist/opencomputeruseagent.brxt"))
    parser.add_argument("--root", required=True, help="an empty folder for the isolated profile")
    args = parser.parse_args()
    run = Run(args)
    try:
        run.main()
    except Exception as error:
        run.check("harness ran", False, repr(error))
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
