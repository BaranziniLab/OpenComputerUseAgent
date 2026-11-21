#!/usr/bin/env python3
"""L4: the WinForms fixture driven through the wrapper on Windows, and the console window check.

    python tests/test_windows_fixture.py --bundle dist/opencomputeruseagent.brxt --report build/windows-fixture.json

Runs on a signed-in Windows desktop (the `windows-latest` runner, or a real Windows 11 machine
before each release). It installs the bundle as Biorouter does, compiles the fixture with Windows
PowerShell's C# compiler, starts it, and drives it with the same assertions as the Linux drive,
read from the fixture's state file. The fixture also counts console windows that appear while it
runs (`console_windows`), so a helper that flashes a PowerShell console fails here.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW = {"action": "accept", "content": {"access": "Allow looking and acting"}}
APP = "DesktopFixture"


def compile_fixture(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    command = (f"Add-Type -OutputType WindowsApplication -OutputAssembly '{output}' "
               "-ReferencedAssemblies System.Windows.Forms,System.Drawing "
               f"-TypeDefinition (Get-Content -Raw '{ROOT / 'fixtures/fixture_winforms.cs'}')")
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True, timeout=300)
    return output


def wait_for(predicate, timeout=20, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def find_index(text, needle):
    for line in text.splitlines():
        stripped = line.strip()
        if needle in stripped and stripped.split(" ", 1)[0].isdigit():
            return stripped.split(" ", 1)[0]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/opencomputeruseagent.brxt")
    parser.add_argument("--report", type=Path, default=ROOT / "build/windows-fixture.json")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    from mcp_client import Client

    report = {"checks": {}, "passed": False}

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        if not ok:
            print(f"    detail: {str(detail)[:800]}", flush=True)
        return ok

    work = Path(tempfile.mkdtemp(prefix="ocua-win-"))
    install = work / "extensions" / "opencomputeruseagent"
    with zipfile.ZipFile(args.bundle) as archive:
        for info in archive.infolist():
            target = install / info.filename
            if not info.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    sync = subprocess.run(["uv", "sync", "--offline"], cwd=install, capture_output=True, text=True, timeout=300)
    check("uv sync with no network", sync.returncode == 0, sync.stderr[-400:])
    exe = compile_fixture(work / "fixture" / "DesktopFixture.exe")
    state_dir = work / "state"
    state_dir.mkdir()
    fixture = subprocess.Popen([str(exe), str(state_dir)])
    client = None
    try:
        check("fixture started", wait_for(lambda: (state_dir / "ready").exists(), 30))

        def state():
            return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))

        env = dict(os.environ, OPENAI_API_KEY="sk-windows-sentinel")
        client = Client(install, env, answer=lambda params: ALLOW,
                        command=["uv", "run", "--offline", "--directory", str(install), "opencomputeruseagent.py"])
        # The first doctor run on a cold CI machine can outlast its 30 s limit: look again.
        for _ in range(3):
            status = json.loads(client.text(client.call("setup_status")))
            if status.get("ready") is True:
                break
        check("setup_status ready", status.get("ready") is True, status)
        text = client.text(client.call("get_app_state", {"app": APP}))
        check("get_app_state", "FixtureInput" in text, text[:300])
        client.call("set_value", {"app": APP, "element_index": find_index(text, "FixtureInput"), "value": "hello"})
        check("set_value", wait_for(lambda: state()["input"] == "hello", 5), state()["input"])
        text = client.text(client.call("get_app_state", {"app": APP}))
        client.call("click", {"app": APP, "element_index": find_index(text, "Apply fixture")})
        check("click", wait_for(lambda: state()["applied"] == "hello", 5), state()["applied"])
        text = client.text(client.call("get_app_state", {"app": APP}))
        refused = client.call("set_value", {"app": APP, "element_index": find_index(text, "FixtureSecret"),
                                            "value": "hunter2"})
        check("set_value into the password field is refused",
              refused.get("isError") and "password_field" in client.text(refused), client.text(refused)[:200])
        # Aimed at the password field by its index, then by a click on it: refused either way, before
        # anything is sent, and the text never lands in the field that holds the keyboard.
        text = client.text(client.call("get_app_state", {"app": APP}))
        typed = client.call("type_text", {"app": APP, "element_index": find_index(text, "FixtureSecret"),
                                          "text": "hunter2"})
        check("type_text aimed at the password field is refused",
              typed.get("isError") and "password_field" in client.text(typed), client.text(typed)[:200])
        # A refused action counts as a change, so the app is read again before the next one.
        text = client.text(client.call("get_app_state", {"app": APP}))
        pressed = client.call("press_key", {"app": APP, "element_index": find_index(text, "FixtureSecret"),
                                            "key": "a"})
        check("press_key aimed at the password field is refused",
              pressed.get("isError") and "password_field" in client.text(pressed), client.text(pressed)[:200])
        text = client.text(client.call("get_app_state", {"app": APP}))
        client.call("click", {"app": APP, "element_index": find_index(text, "FixtureSecret")})
        client.call("get_app_state", {"app": APP})
        typed = client.call("type_text", {"app": APP, "text": "hunter2"})
        check("type_text after clicking the password field is refused",
              typed.get("isError") and "password_field" in client.text(typed), client.text(typed)[:200])
        time.sleep(0.5)
        check("secret_length stays 0", state()["secret_length"] == 0, state()["secret_length"])
        check("text aimed at the password field never lands in another field", state()["input"] == "hello",
              state()["input"])
        # Aimed at the ordinary field while another control holds the keyboard: it lands in that field.
        text = client.text(client.call("get_app_state", {"app": APP}))
        client.call("click", {"app": APP, "element_index": find_index(text, "FixtureScroll")})
        text = client.text(client.call("get_app_state", {"app": APP}))
        typed = client.call("type_text", {"app": APP, "element_index": find_index(text, "FixtureInput"),
                                          "text": " world"})
        check("type_text aimed at a field types into that field",
              wait_for(lambda: state()["input"] == "hello world", 5) and state()["secret_length"] == 0,
              {"input": state()["input"], "secret_length": state()["secret_length"],
               "answer": client.text(typed)[:200]})
        text = client.text(client.call("get_app_state", {"app": APP}))
        client.call("scroll", {"app": APP, "element_index": find_index(text, "FixtureScroll"), "direction": "down"})
        check("scroll", wait_for(lambda: state()["scroll"] > 0, 5), state()["scroll"])
        captured = client.call("screen_capture", {"window_title": "Desktop Fixture Drop"})
        check("window capture", any(b.get("type") == "image" for b in captured.get("content", [])),
              client.text(captured)[:200])
        check("display capture refused", "Whole display" in client.text(client.call("screen_capture", {"display": 0})))
        check("paste chord refused",
              "Copy, cut and paste" in client.text(client.call("press_key", {"app": APP, "key": "ctrl+v"})))
        check("no console window appeared", state().get("console_windows", 0) == 0, state().get("console_windows"))
        report["passed"] = all(item["ok"] for item in report["checks"].values())
    finally:
        if client is not None:
            client.close()
        fixture.terminate()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
