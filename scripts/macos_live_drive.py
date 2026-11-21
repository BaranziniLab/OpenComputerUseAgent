#!/usr/bin/env python3
"""L5: the macOS live drive. The owner runs it from Terminal on their own Mac; an agent never does.

    python3 scripts/macos_live_drive.py --bundle dist/opencomputeruseagent.brxt [--onboarding] [--allow-adhoc]

It needs the owner to grant Accessibility and Screen Recording to the helper app "Open Computer
Use Agent", and the owner does every grant and revoke by hand. This script never opens, edits or
resets a privacy setting itself; with --onboarding it asks the helper to open its own permission
window, which only links to the System Settings panes.

What it does:
  1. Installs the bundle as Biorouter does (plain file extraction, then `uv sync` offline) into
     an isolated folder outside any app bundle, and checks the helper app's signature
     (`codesign --verify --strict --deep`, `spctl`, `stapler validate`). An ad hoc build is
     refused unless --allow-adhoc: grants given to it are tied to that build.
  2. Starts the wrapper the way Biorouter does and calls setup_status, which must name the
     helper app, not Biorouter, for both permissions. Waits for the owner's grants.
  3. Builds and starts the AppKit fixture (fixtures/fixture_macos.swift) and drives it through
     the wrapper: get_app_state, set_value, click, type_text, the password field (refused, and
     secret_length stays 0), scroll, window capture, the paste chord and display capture
     (refused), Biorouter's own windows and System Settings' privacy pane (refused, look only), a
     batch of two actions (the second refused unrun) and a cancel (the next call asks again).
  4. Reads every outcome from the fixture's state file, never from a tool's answer.
  5. Stops the fixture and the helper's app agent by pid (exact executable path, parent launchd).

Results go to build/macos-live/<version>.json, which `scripts/release.sh publish` requires.
"""
import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))
TEAM_ID = "F3YYBXAFJ8"
IDENTIFIER = "edu.ucsf.baranzinilab.opencomputeruseagent"
ALLOW = {"action": "accept", "content": {"access": "Allow looking and acting"}}
FIXTURE_APP = "Desktop Fixture"


def sh(*args, **kwargs):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, **kwargs)


def extract_like_biorouter(bundle, destination):
    if destination.exists():
        shutil.rmtree(destination)
    with zipfile.ZipFile(bundle) as archive:
        for info in archive.infolist():
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))


def build_fixture(work):
    bundle = work / f"{FIXTURE_APP}.app"
    (bundle / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    built = sh("xcrun", "swiftc", "-O", "-target", f"{os.uname().machine}-apple-macos13.0", "-o",
               bundle / "Contents/MacOS/DesktopFixture", ROOT / "fixtures/fixture_macos.swift")
    if built.returncode != 0:
        raise SystemExit("fixture build failed:\n" + built.stderr[-2000:])
    info = {"CFBundleIdentifier": "edu.ucsf.baranzinilab.desktop-fixture", "CFBundleName": FIXTURE_APP,
            "CFBundleDisplayName": FIXTURE_APP, "CFBundleExecutable": "DesktopFixture", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "LSMinimumSystemVersion": "13.0", "NSPrincipalClass": "NSApplication"}
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    sh("codesign", "--force", "--sign", "-", bundle)
    return bundle


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
    parser.add_argument("--root", type=Path, default=Path.home() / "bi-iso/ocu-ext/mac-live-drive")
    parser.add_argument("--onboarding", action="store_true", help="open the helper's permission window")
    parser.add_argument("--allow-adhoc", action="store_true", help="accept an ad hoc signed helper")
    parser.add_argument("--grant-timeout", type=int, default=600)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("this drive is for macOS")
    from mcp_client import Client
    from opencomputeruseagent import __version__
    from opencomputeruseagent.dispatch import end_app_agents
    from opencomputeruseagent.payload import target as detect_target

    report = {"version": __version__, "checks": {}, "passed": False}

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        return ok

    install = args.root / "extensions/opencomputeruseagent"
    extract_like_biorouter(args.bundle.resolve(), install)
    target = detect_target()
    payload = install / "payload" / target / __version__
    app = payload / "Open Computer Use Agent.app"
    os.chmod(app / "Contents/MacOS/ocu", 0o755)
    signature = sh("codesign", "-dv", "--verbose=4", app).stderr
    check("signature verifies after plain extraction", sh("codesign", "--verify", "--strict", "--deep", app).returncode == 0)
    check("bundle identifier", f"Identifier={IDENTIFIER}" in signature, signature[-400:])
    team_signed = f"TeamIdentifier={TEAM_ID}" in signature
    if not team_signed and not args.allow_adhoc:
        raise SystemExit("this helper is not signed with the release team identity; pass --allow-adhoc to drive an "
                         "ad hoc build, and remove its grants from System Settings afterwards")
    if team_signed:
        check("Gatekeeper accepts it", sh("spctl", "-a", "-vv", "-t", "exec", app).returncode == 0)
        check("notarization ticket stapled", sh("xcrun", "stapler", "validate", app).returncode == 0)
    sync = sh("uv", "sync", "--offline", cwd=install)
    check("uv sync with no network", sync.returncode == 0, sync.stderr[-400:])

    work = Path(tempfile.mkdtemp(prefix="ocua-live-"))
    fixture = None
    env = dict(os.environ, OPENAI_API_KEY="sk-live-drive-sentinel")
    client = Client(install, env, answer=lambda params: ALLOW,
                    command=["uv", "run", "--offline", "--directory", str(install), "opencomputeruseagent.py"])
    try:
        status = json.loads(client.text(client.call("setup_status")))
        report["setup_status_before"] = status
        names = json.dumps(status.get("doctor", {}))
        check("permissions name the helper, not Biorouter",
              "Open Computer Use Agent" in names and "for Biorouter" not in names, status.get("doctor"))
        if not status.get("ready"):
            if args.onboarding:
                print(client.text(client.call("open_permission_setup")))
            print('Grant Accessibility and Screen Recording to "Open Computer Use Agent" in System Settings.')
            ready = wait_for(lambda: json.loads(client.text(client.call("setup_status"))).get("ready"),
                             args.grant_timeout, 5)
            if not check("owner granted both permissions", ready):
                return 1
        bundle = build_fixture(work)
        state_dir = work / "fixture"
        state_dir.mkdir()
        fixture = subprocess.Popen([str(bundle / "Contents/MacOS/DesktopFixture"), str(state_dir)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        check("fixture started", wait_for(lambda: (state_dir / "ready").exists(), 30))

        def state():
            return json.loads((state_dir / "state.json").read_text())

        query = state()["app"] if "app" in state() else FIXTURE_APP
        text = client.text(client.call("get_app_state", {"app": query}))
        check("get_app_state", "FixtureInput" in text, text[:300])
        check("one card", len(client.cards) == 1)
        index = find_index(text, "FixtureInput")
        client.call("set_value", {"app": query, "element_index": index, "value": "hello"})
        check("set_value", wait_for(lambda: state()["input"] == "hello", 5), state()["input"])
        text = client.text(client.call("get_app_state", {"app": query}))
        client.call("click", {"app": query, "element_index": find_index(text, "Apply fixture")})
        check("click", wait_for(lambda: state()["applied"] == "hello", 5), state()["applied"])
        text = client.text(client.call("get_app_state", {"app": query}))
        secret = find_index(text, "FixtureSecret")
        refused = client.call("set_value", {"app": query, "element_index": secret, "value": "hunter2"})
        check("set_value into the password field is refused", refused.get("isError"), client.text(refused)[:200])
        text = client.text(client.call("get_app_state", {"app": query}))
        client.call("click", {"app": query, "element_index": find_index(text, "FixtureSecret")})
        client.call("get_app_state", {"app": query})
        typed = client.call("type_text", {"app": query, "text": "hunter2"})
        check("type_text into the password field is refused", typed.get("isError"), client.text(typed)[:200])
        time.sleep(0.5)
        check("secret_length stays 0", state()["secret_length"] == 0, state()["secret_length"])
        text = client.text(client.call("get_app_state", {"app": query}))
        client.call("scroll", {"app": query, "element_index": find_index(text, "FixtureScroll"), "direction": "down"})
        check("scroll", wait_for(lambda: state()["scroll"] > 0, 5), state()["scroll"])
        captured = client.call("screen_capture", {"window_title": "Desktop Fixture Drop"})
        check("window capture", any(b.get("type") == "image" for b in captured.get("content", [])),
              client.text(captured)[:200])
        check("display capture refused", "Whole display" in client.text(client.call("screen_capture", {"display": 0})))
        check("paste chord refused", "Copy, cut and paste" in client.text(client.call("press_key", {"app": query, "key": "cmd+v"})))
        check("Biorouter refused", "Biorouter itself" in client.text(client.call("get_app_state", {"app": "Biorouter"})))
        # A batch (Biorouter sends a reply's calls at once): the second action is refused unrun.
        text = client.text(client.call("get_app_state", {"app": query}))
        before = state()
        meta = {"biorouter-session-id": "chat-1", "progressToken": 1}
        first = client.send("tools/call", {"name": "click", "arguments": {
            "app": query, "element_index": find_index(text, "Fixture option")}, "_meta": meta})
        second = client.send("tools/call", {"name": "set_value", "arguments": {
            "app": query, "element_index": find_index(text, "FixtureInput"), "value": "never"}, "_meta": meta})
        refused, ran = client.wait(second)["result"], client.wait(first)["result"]
        check("an action sent while another runs is refused",
              refused.get("isError") and "still running" in client.text(refused), client.text(refused)[:200])
        check("the refused action never reached the app", state()["input"] == before["input"], state()["input"])
        # A cancel: not answered, the chat's access ends, the next call asks again.
        text = client.text(client.call("get_app_state", {"app": query}))
        request_id = client.send("tools/call", {"name": "scroll", "arguments": {
            "app": query, "element_index": find_index(text, "FixtureScroll"), "direction": "up"}, "_meta": meta})
        client.notify("notifications/cancelled", {"requestId": request_id, "reason": "operation cancelled"})
        time.sleep(3)
        check("a cancelled call is not answered", not client.answered(request_id))
        client.call("get_app_state", {"app": query})
        check("after a cancel the next call asks again", len(client.cards) == 2, len(client.cards))
        settings = client.call("get_app_state", {"app": "System Settings"})
        report["system_settings_look"] = not settings.get("isError")
        report["passed"] = all(item["ok"] for item in report["checks"].values())
    finally:
        client.close()
        if fixture is not None:
            try:
                os.killpg(fixture.pid, signal.SIGTERM)
            except OSError:
                pass
        ended = end_app_agents([payload])
        report["ended_app_agents"] = ended
        out = ROOT / "build/macos-live"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{__version__}.json").write_text(json.dumps(report, indent=2, default=str))
        print("report:", out / f"{__version__}.json")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
