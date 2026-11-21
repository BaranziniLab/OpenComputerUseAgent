import os

import pytest

from opencomputeruseagent import guard, procs
from opencomputeruseagent.guard import Facts, Identity


@pytest.mark.parametrize("facts", [
    Facts("SecurityAgent", "com.apple.SecurityAgent", 1), Facts("Keychain Access", "com.apple.keychainaccess", 2),
    Facts("consent.exe", None, 3), Facts("pinentry-gnome3", None, 4), Facts("gcr-prompter", None, 5),
    Facts("NotificationCenter", "com.apple.notificationcenterui", 6), Facts("LockApp", None, 7),
])
def test_security_prompts_are_refused(facts):
    assert guard.is_security_prompt(facts)


@pytest.mark.parametrize("facts", [
    Facts("Terminal", "com.apple.Terminal", 1), Facts("iTerm2", "com.googlecode.iterm2", 2),
    Facts("WindowsTerminal.exe", None, 3), Facts("gnome-terminal-server", None, 4),
    Facts("Ghostty", "com.mitchellh.ghostty", 5), Facts("x", "org.gnome.Console", 6),
])
def test_terminals_are_look_and_click_only(facts):
    assert guard.is_terminal(facts)
    assert set(guard.KEYBOARD_LIKE) == {"type_text", "press_key", "set_value", "drag", "perform_secondary_action"}


def test_ordinary_apps_pass():
    for facts in (Facts("TextEdit", "com.apple.TextEdit", 9), Facts("gedit", None, 10), Facts("notepad.exe", None, 11)):
        assert not guard.is_security_prompt(facts) and not guard.is_terminal(facts)


def test_privacy_settings_are_look_only():
    settings = Facts("System Settings", "com.apple.systempreferences", 1)
    assert guard.is_privacy_settings(settings, "Privacy & Security")
    assert guard.is_privacy_settings(settings, None)  # unknown pane: refuse
    assert not guard.is_privacy_settings(settings, "Displays")
    assert guard.is_privacy_settings(Facts("SecHealthUI", None, 2), "Home")
    assert not guard.is_privacy_settings(Facts("TextEdit", None, 3), "Privacy notes")


def test_own_bundles_including_the_helper_app():
    for bundle in ("com.electron.biorouter", "edu.ucsf.biorouter.helper", "org.biorouter.computer-use",
                   "edu.ucsf.baranzinilab.opencomputeruseagent"):
        assert Identity().owns(Facts("x", bundle, 0))
    assert not Identity().owns(Facts("TextEdit", "com.apple.TextEdit", 5))
    assert Identity().owns(Facts("Biorouter", None, 0))


def test_own_windows_by_ancestry_and_install_folder():
    rows = [
        procs.Process(1, 0, "/sbin/launchd"),
        procs.Process(100, 1, "/Applications/Biorouter.app/Contents/MacOS/Biorouter"),
        procs.Process(200, 100, "/Applications/Biorouter.app/Contents/Resources/bin/biorouterd"),
        procs.Process(300, 200, "/opt/homebrew/bin/uv"),
        procs.Process(os.getpid(), 300, "/ext/.venv/bin/python3"),
        procs.Process(400, 100, "/Applications/Biorouter.app/Contents/Frameworks/Biorouter Helper (Renderer).app/Contents/MacOS/Biorouter Helper (Renderer)"),
        procs.Process(500, 1, "/Applications/TextEdit.app/Contents/MacOS/TextEdit"),
        procs.Process(600, 1, "/ext/payload/darwin-arm64/0.1.0/Open Computer Use Agent.app/Contents/MacOS/ocu"),
    ]
    identity = Identity.current(helper_paths=["/ext/payload"], rows=rows)
    assert identity.owns_pid(100) and identity.owns_pid(200) and identity.owns_pid(300)
    assert identity.owns_pid(400)  # inside the running Biorouter's install folder
    assert identity.owns_pid(600)  # this extension's helper app
    assert not identity.owns_pid(500)


def test_desktop_main_pid_and_its_descendants(monkeypatch):
    rows = [procs.Process(1, 0, ""), procs.Process(10, 1, "/usr/lib/biorouter/Biorouter"),
            procs.Process(11, 10, "/usr/lib/biorouter/Biorouter --type=renderer"),
            procs.Process(os.getpid(), 1, "/usr/bin/python3"), procs.Process(12, 1, "/usr/bin/gedit")]
    monkeypatch.setenv("BIOROUTER_DESKTOP_MAIN_PID", "10")
    identity = Identity.current(rows=rows)
    assert identity.owns_pid(10) and identity.owns_pid(11) and not identity.owns_pid(12)


def test_install_root():
    from pathlib import Path
    assert guard.install_root("/Applications/Biorouter.app/Contents/Resources/bin/biorouterd") == Path("/Applications/Biorouter.app")
    assert guard.install_root("/opt/Biorouter/resources/bin/biorouterd") == Path("/opt/Biorouter")
    assert guard.install_root("/usr/bin/biorouterd") is None


@pytest.mark.parametrize("text, kind", [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "private key"),
    ("AKIAABCDEFGHIJKLMNOP", "AWS key"),
    ("ghp_" + "a" * 36, "API token"),
    ("sk-ant-api03-" + "x" * 30, "API token"),
    ("sk-proj-" + "y" * 30, "API token"),
    ("xoxb-1234567890-abc", "API token"),
    ("AIza" + "B" * 35, "API token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "JSON web token"),
])
def test_secrets_are_refused(text, kind):
    assert kind in guard.secret_kinds(text)
    assert "looks like a secret" in guard.secret_refusal(text)


def test_ordinary_text_is_not_a_secret():
    for text in ("a quarterly report", "Meeting at 3 pm", "sk-short", "hello@example.com"):
        assert guard.secret_refusal(text) is None


@pytest.mark.parametrize("key", ["cmd+c", "ctrl+v", "Ctrl+X", "super+c", "cmd+shift+v", "shift+Insert",
                                 "ctrl+insert", "shift+Delete", "command+v", "cmd-c"])
def test_clipboard_chords_are_refused(key):
    assert guard.is_clipboard_chord(key)


@pytest.mark.parametrize("key", ["c", "Return", "cmd+a", "ctrl+s", "alt+c", "Delete", "shift+a", "-", "cmd+-"])
def test_other_keys_pass(key):
    assert not guard.is_clipboard_chord(key)


def test_facts_from_state_headers():
    mac = 'App=com.apple.TextEdit (pid 812)\nWindow: "Untitled", App: TextEdit.\n\t0 window Untitled'
    facts, title = guard.facts_from_state(mac)
    assert facts == Facts("TextEdit", "com.apple.TextEdit", 812) and title == "Untitled"
    linux = 'App=gedit (pid 162)\nWindow: "Doc.txt - gedit", App: gedit.\n'
    facts, title = guard.facts_from_state(linux)
    assert facts == Facts("gedit", None, 162) and title == "Doc.txt - gedit"
    assert guard.facts_from_state("no header") == (None, None)


def test_facts_from_list_lines():
    assert guard.facts_from_list_line("TextEdit — com.apple.TextEdit [running, last-used=2026-11-20]") == Facts("TextEdit", "com.apple.TextEdit", 0)
    assert guard.facts_from_list_line("gedit -- gedit [running, pid=162, window=Doc [draft]]") == Facts("gedit", None, 162)
    assert guard.facts_from_list_line("garbage") is None


def test_query_names_protected():
    for query in ("Biorouter", "com.electron.biorouter", "SecurityAgent", "Open Computer Use Agent",
                  "edu.ucsf.baranzinilab.opencomputeruseagent", "biorouterd"):
        assert guard.query_names_protected(query), query
    assert not guard.query_names_protected("TextEdit")


def test_secure_input_is_false_off_macos(monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "linux")
    assert guard.secure_input_enabled() is False
