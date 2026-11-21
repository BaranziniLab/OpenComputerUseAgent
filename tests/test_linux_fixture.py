#!/usr/bin/env python3
"""L3: the GTK fixture driven through the wrapper, as Biorouter runs it, on real distros.

    python3 tests/test_linux_fixture.py --docker                       # every distro image
    python3 tests/test_linux_fixture.py --docker --distro ubuntu2204   # one
    python3 tests/test_linux_fixture.py --inside --bundle <brxt> --report <json>   # in a session

On the host, `--docker` starts one container per distro image (tests/docker/Dockerfile.<distro>,
built when missing) and runs this script inside it, as an ordinary user, under
`xvfb-run -a dbus-run-session` with openbox. Inside, it installs the .brxt the way Biorouter
does (plain file extraction, then `uv sync` with no network), starts the wrapper with
`uv run --directory <install> opencomputeruseagent.py`, answers the consent card, and drives
the fixture. Every outcome is read from the fixture's own state file, never from a tool's
answer. Typing into and setting the password field must be refused, and secret_length must stay 0.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISTROS = ["ubuntu2204", "debian12", "rocky9", "ubuntu2404"]
ALLOW = {"action": "accept", "content": {"access": "Allow looking and acting"}}
APP = "desktop-fixture"


# --------------------------------------------------------------------------- host side
def docker_image(distro, platform=None):
    name = f"ocua-fixture-{distro}" + ("-amd64" if platform == "linux/amd64" else "")
    probe = subprocess.run(["docker", "image", "ls", "-q", name], capture_output=True, text=True)
    if not probe.stdout.strip():
        args = ["docker", "build", "-t", name, "-f", str(ROOT / "tests/docker" / f"Dockerfile.{distro}")]
        if platform:
            args += ["--platform", platform]
        subprocess.run(args + [str(ROOT / "tests/docker")], check=True)
    return name


def run_docker(distro, bundle, out_dir, platform=None):
    image = docker_image(distro, platform)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o777)
    inner = (
        "set -e; cp /bundle/opencomputeruseagent.brxt /home/tester/b.brxt; chown tester /home/tester/b.brxt; "
        "su tester -c 'cd /home/tester && timeout 600 xvfb-run -a -s \"-screen 0 1280x1024x24\" "
        "dbus-run-session -- python3 /repo/tests/test_linux_fixture.py --inside "
        f"--bundle /home/tester/b.brxt --report /out/{distro}.json' > /out/{distro}.log 2>&1; "
        f"echo rc=$? >> /out/{distro}.log"
    )
    args = ["docker", "run", "--rm", "--network", "none", "-v", f"{ROOT}:/repo:ro",
            "-v", f"{bundle.parent}:/bundle:ro", "-v", f"{out_dir}:/out"]
    if platform:
        args += ["--platform", platform]
    subprocess.run(args + [image, "sh", "-c", inner], check=False, timeout=900)
    report_path = out_dir / f"{distro}.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {"passed": False, "error": "no report"}
    return report


# --------------------------------------------------------------------------- inside a session
def extract_like_biorouter(bundle, destination):
    """`BrxtBundle::extract_to`: plain files, no modes, no symlinks, nothing removed first."""
    with zipfile.ZipFile(bundle) as archive:
        for info in archive.infolist():
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(archive.read(info))


def wait_for(predicate, timeout=20, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def find_index(state_text, needle):
    for line in state_text.splitlines():
        stripped = line.strip()
        if needle in stripped:
            first = stripped.split(" ", 1)[0]
            if first.isdigit():
                return first, stripped
    return None, None


def frame_center(line):
    import re

    match = re.search(r"Frame: \{x: (-?[\d.]+), y: (-?[\d.]+), width: ([\d.]+), height: ([\d.]+)\}", line or "")
    if not match:
        return None
    x, y, w, h = (float(v) for v in match.groups())
    return x + w / 2, y + h / 2


def helper_processes():
    """Helper processes by executable, or by argv[0] when the helper runs under an emulator
    (there /proc/<pid>/exe names the emulator)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            exe = ""
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            argv = []
        names = [exe] + [a.decode(errors="replace") for a in argv[:2]]
        if any(name.endswith("/ocu") for name in names):
            found.append(int(entry))
    return found


def inside(bundle, report_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from mcp_client import Client

    report = {"checks": {}, "passed": False}

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        return ok

    work = Path(tempfile.mkdtemp(prefix="ocua-fixture-"))
    runtime = work / "runtime"
    runtime.mkdir(mode=0o700)
    # The person's own variables reach every extension; none of them may reach the helper.
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), XDG_SESSION_TYPE="x11",
               OPENAI_API_KEY="sk-fixture-sentinel", BIOROUTER_PORT="9")
    install = Path.home() / "extensions" / "opencomputeruseagent"
    processes = []
    try:
        extract_like_biorouter(bundle, install)
        sync_env = dict(env, UV_OFFLINE="1", UV_CACHE_DIR=str(work / "uv-cache"), UV_PYTHON_DOWNLOADS="never")
        sync = subprocess.run(["uv", "sync"], cwd=install, env=sync_env, capture_output=True, text=True, timeout=300)
        check("uv sync with no network", sync.returncode == 0, (sync.stdout + sync.stderr)[-800:])
        report["python"] = subprocess.run([str(install / ".venv/bin/python"), "--version"], capture_output=True,
                                          text=True).stdout.strip()
        processes.append(subprocess.Popen(["openbox", "--sm-disable"], env=env, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True))
        time.sleep(1.0)
        fixture_work = work / "fixture"
        fixture_work.mkdir()
        processes.append(subprocess.Popen(["/usr/bin/python3", str(ROOT / "fixtures/fixture_gtk.py"), str(fixture_work)],
                                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                          start_new_session=True))
        if not check("fixture started", wait_for(lambda: (fixture_work / "ready").exists(), 30)):
            raise RuntimeError("fixture did not start")

        def state():
            return json.loads((fixture_work / "state.json").read_text())

        client = Client(install, env, answer=lambda params: ALLOW,
                        command=["uv", "run", "--offline", "--directory", str(install), "opencomputeruseagent.py"])
        try:
            report["initialize"] = client.initialize.get("serverInfo")
            listed = client.request("tools/list", {})["tools"]
            check("twelve tools", len(listed) == 12, [t["name"] for t in listed])
            status = json.loads(client.text(client.call("setup_status")))
            check("setup_status ready", status.get("ready") is True, status)
            check("no card for setup_status", client.cards == [])

            apps = client.text(client.call("list_apps"))
            check("list_apps shows the fixture", APP in apps, apps[-400:])
            check("one card before the first look", len(client.cards) == 1)
            looked = client.call("get_app_state", {"app": APP})
            text = client.text(looked)
            check("get_app_state", not looked.get("isError") and "FixtureInput" in text, text[:300])
            check("result is framed", text.startswith(f'[Content read from the app "{APP}"'))
            check("still one card", len(client.cards) == 1)

            pids = helper_processes()
            leaked = []
            for pid in pids:
                try:
                    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                except OSError:
                    continue
                pairs = dict(item.decode(errors="replace").split("=", 1) for item in environ if b"=" in item)
                leaked += [k for k in pairs if k.endswith("_API_KEY") or k.startswith("BIOROUTER_") or k == "VIRTUAL_ENV"]
                check("helper PATH is the system one", pairs.get("PATH", "").startswith("/usr/bin:"), pairs.get("PATH"))
            check("helper environment scrubbed", pids and not leaked, {"pids": pids, "leaked": leaked})

            index, _ = find_index(text, "FixtureInput")
            result = client.call("set_value", {"app": APP, "element_index": index, "value": "hello"})
            check("set_value", wait_for(lambda: state()["input"] == "hello", 5), client.text(result)[:200])

            text = client.text(client.call("get_app_state", {"app": APP}))
            index, line = find_index(text, "FixtureInput")
            client.call("click", {"app": APP, "element_index": index})
            text = client.text(client.call("get_app_state", {"app": APP}))
            result = client.call("type_text", {"app": APP, "text": " world"})
            check("type_text goes to the focused field", wait_for(lambda: state()["input"] == "hello world", 5),
                  {"input": state()["input"], "answer": client.text(result)[:200]})

            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "push button Apply fixture")
            client.call("click", {"app": APP, "element_index": index})
            check("click", wait_for(lambda: state()["applied"] == "hello world", 5), state()["applied"])

            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "check box Fixture option")
            client.call("click", {"app": APP, "element_index": index})
            check("checkbox", wait_for(lambda: state()["checked"] is True, 5))

            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "password text FixtureSecret")
            refused = client.call("set_value", {"app": APP, "element_index": index, "value": "hunter2"})
            check("set_value into the password field is refused",
                  refused.get("isError") and "password_field" in client.text(refused), client.text(refused)[:200])
            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "password text FixtureSecret")
            clicked = client.call("click", {"app": APP, "element_index": index})
            text = client.text(client.call("get_app_state", {"app": APP}))
            focused_secret = "The focused UI element is password text FixtureSecret" in text
            report["secret_focused_after_click"] = focused_secret
            typed = client.call("type_text", {"app": APP, "text": "hunter2"})
            if focused_secret:
                check("type_text into the focused password field is refused",
                      typed.get("isError") and "password_field" in client.text(typed), client.text(typed)[:200])
                text = client.text(client.call("get_app_state", {"app": APP}))
                pressed = client.call("press_key", {"app": APP, "key": "a"})
                check("press_key while the password field has focus is refused",
                      pressed.get("isError") and "password_field" in client.text(pressed), client.text(pressed)[:200])
            time.sleep(0.5)
            check("secret_length stays 0", state()["secret_length"] == 0, state()["secret_length"])

            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "text FixtureScroll")
            client.call("scroll", {"app": APP, "element_index": index, "direction": "down", "pages": 1})
            check("scroll", wait_for(lambda: state()["scroll"] > 0, 5), state()["scroll"])

            text = client.text(client.call("get_app_state", {"app": APP}))
            _, alpha = find_index(text, "table cell Alpha")
            _, gamma = find_index(text, "table cell Gamma")
            start, end = frame_center(alpha), frame_center(gamma)
            if start and end:
                dragged = client.call("drag", {"app": APP, "from_x": start[0], "from_y": start[1],
                                               "to_x": end[0], "to_y": end[1]})
                moved = wait_for(lambda: state()["order"] != ["Alpha", "Beta", "Gamma"], 5)
                report["drag"] = {"answer_error": bool(dragged.get("isError")), "order": state()["order"],
                                  "answer": client.text(dragged)[:200]}
                check("drag was delivered", not dragged.get("isError"), report["drag"])
                check("drag reordered the list", moved, report["drag"])
            check("pointer released after drag", wait_for(lambda: not state()["pointer_pressed"], 3))

            captured = client.call("screen_capture", {"window_title": "Drop"})
            images = [b for b in captured.get("content", []) if b.get("type") == "image"]
            check("window capture", not captured.get("isError") and images and len(images[0]["data"]) > 200,
                  client.text(captured)[:200])
            display = client.call("screen_capture", {"display": 0})
            check("display capture is refused", display.get("isError") and "Whole display" in client.text(display))
            chord = client.call("press_key", {"app": APP, "key": "ctrl+v"})
            check("paste chord is refused", chord.get("isError") and "Copy, cut and paste" in client.text(chord))
            secret = client.call("type_text", {"app": APP, "text": "ghp_" + "a" * 36})
            check("secret text is refused", secret.get("isError") and "looks like a secret" in client.text(secret))
            check("one card for the whole drive", len(client.cards) == 1, len(client.cards))

            # A batch: Biorouter sends a reply's calls at once. The second action, sent while the
            # first is still running, is refused and never reaches the app.
            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "check box Fixture option")
            before = state()
            meta = {"biorouter-session-id": "chat-1", "progressToken": 1}
            first = client.send("tools/call", {"name": "click", "arguments": {"app": APP, "element_index": index},
                                               "_meta": meta})
            second = client.send("tools/call", {"name": "set_value", "arguments": {
                "app": APP, "element_index": find_index(text, "FixtureInput")[0], "value": "never"}, "_meta": meta})
            refused, ran = client.wait(second)["result"], client.wait(first)["result"]
            check("an action sent while another runs is refused",
                  refused.get("isError") and "Another action in this chat was still running" in client.text(refused),
                  client.text(refused)[:200])
            check("the running action finished", not ran.get("isError")
                  and wait_for(lambda: state()["checked"] != before["checked"], 5), client.text(ran)[:200])
            check("the refused action never reached the app", state()["input"] == before["input"], state()["input"])

            # A cancel (Biorouter sends one on its own timeout, and in any version that passes Stop
            # on): the call is not answered, the helper is ended, and the next call asks again.
            text = client.text(client.call("get_app_state", {"app": APP}))
            index, _ = find_index(text, "text FixtureScroll")
            running = helper_processes()
            request_id = client.send("tools/call", {"name": "scroll", "arguments": {
                "app": APP, "element_index": index, "direction": "up", "pages": 1}, "_meta": meta})
            client.notify("notifications/cancelled", {"requestId": request_id, "reason": "operation cancelled"})
            time.sleep(3)
            check("a cancelled call is not answered", not client.answered(request_id))
            check("the cancel ended the helper", running and wait_for(lambda: not set(helper_processes()) & set(running), 5),
                  {"before": running, "after": helper_processes()})
            looked = client.call("get_app_state", {"app": APP})
            check("after a cancel the next call asks again", not looked.get("isError") and len(client.cards) == 2,
                  {"cards": len(client.cards), "answer": client.text(looked)[:200]})
        finally:
            client.close()
        check("no helper left after the wrapper exits", wait_for(lambda: not helper_processes(), 10),
              helper_processes())
        report["passed"] = all(item["ok"] for item in report["checks"].values())
    except Exception as error:  # the report says what broke
        report["error"] = repr(error)
    finally:
        for process in reversed(processes):
            try:
                os.killpg(process.pid, 15)
            except OSError:
                pass
        Path(report_path).write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "failed": [k for k, v in report["checks"].items() if not v["ok"]],
                      "error": report.get("error")}))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--distro", action="append", choices=DISTROS)
    parser.add_argument("--platform", help="e.g. linux/amd64 to run the x64 helper under emulation")
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/opencomputeruseagent.brxt")
    parser.add_argument("--out", type=Path, default=ROOT / "build/fixture-reports")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.inside:
        return inside(args.bundle, args.report or Path("report.json"))
    if not args.docker:
        parser.error("pass --docker (host) or --inside (in a desktop session)")
    results = {}
    for distro in args.distro or DISTROS:
        label = distro + ("-amd64" if args.platform == "linux/amd64" else "")
        report = run_docker(distro, args.bundle.resolve(), args.out / label, args.platform)
        results[label] = {"passed": report.get("passed"), "error": report.get("error"),
                          "failed": [k for k, v in report.get("checks", {}).items() if not v["ok"]]}
        print(label, json.dumps(results[label]), flush=True)
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
