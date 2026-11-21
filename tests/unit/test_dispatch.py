import json
import threading
import time

import pytest

from conftest import SESSION_ENV, needs_posix, write_payload
from opencomputeruseagent import consent, guard, host
from opencomputeruseagent.desktop_lock import DesktopLock
from opencomputeruseagent.dispatch import CANCELLED_CALL, OVERLAP_REFUSAL, Dispatcher

pytestmark = needs_posix

ALLOW = {"action": "accept", "content": {"access": consent.LOOK_AND_ACT}}
LOOK_ONLY = {"action": "accept", "content": {"access": consent.LOOK_ONLY}}


class StaticIdentity:
    def __init__(self, own_pids=()):
        self.identity = guard.Identity(pids=own_pids)

    def get(self):
        return self.identity


class Rig:
    def __init__(self, tmp_path, answers=(ALLOW,), own_pids=(), question=None, **env):
        write_payload(tmp_path)
        self.answers = list(answers)
        self.cards = []
        environ = dict(SESSION_ENV, HOME=str(tmp_path), **env)
        self.lock = DesktopLock(tmp_path / "lock" / "computer-use.desktop.lock")
        self.d = Dispatcher(ask=self.ask, environ=environ, root=tmp_path, lock=self.lock, target="linux-x64",
                            identity=StaticIdentity(own_pids), question_refusal=lambda: question)
        self.d.grants.can_ask = True

    def ask(self, params, cancel):
        self.cards.append(params)
        return self.answers.pop(0) if self.answers else {"action": "decline"}

    def call(self, name, arguments=None, session="chat-1", cancel=None, ticket=None):
        return self.d.call(name, arguments or {}, session, cancel, ticket=ticket)

    def close(self):
        self.d.shutdown()


@pytest.fixture
def rig(tmp_path, pass_fake_variables):
    made = []

    def make(**kwargs):
        made.append(Rig(tmp_path, **kwargs))
        return made[-1]

    yield make
    for r in made:
        r.close()


def text(result):
    return "\n".join(b["text"] for b in result["content"] if b["type"] == "text")


def test_one_card_then_look_and_act(rig):
    r = rig()
    state = r.call("get_app_state", {"app": "Editor"})
    assert not state.get("isError"), text(state)
    assert text(state).startswith('[Content read from the app "Editor"')
    assert any(b["type"] == "image" for b in state["content"])
    clicked = r.call("click", {"app": "Editor", "element_index": "0"})
    assert not clicked.get("isError"), text(clicked)
    assert len(r.cards) == 1 and r.lock.held


def test_acting_needs_a_fresh_look_in_this_chat(rig):
    r = rig()
    refused = r.call("click", {"app": "Editor", "element_index": "0"})
    assert refused["isError"] and "Run get_app_state" in text(refused)


def test_a_failed_action_needs_a_new_look(rig):
    r = rig(FAKE_FAIL="click")
    r.call("get_app_state", {"app": "Editor"})
    failed = r.call("click", {"app": "Editor", "element_index": "0"})
    assert failed["isError"]
    again = r.call("type_text", {"app": "Editor", "text": "hello"})
    assert again["isError"] and "Run get_app_state" in text(again)


def test_biorouters_own_window_is_withheld(rig):
    r = rig(own_pids=(4242,))
    result = r.call("get_app_state", {"app": "Editor"})
    assert result["isError"] and "Biorouter itself" in text(result)
    assert "App=Editor" not in json.dumps(result)


def test_naming_biorouter_is_refused_before_any_card(rig):
    r = rig()
    for app in ("Biorouter", "com.electron.biorouter", "Open Computer Use Agent"):
        result = r.call("get_app_state", {"app": app})
        assert result["isError"] and "Biorouter itself" in text(result)
    result = r.call("get_app_state", {"app": "SecurityAgent"})
    assert "security or password prompt" in text(result)
    assert r.cards == []


def test_list_apps_leaves_out_biorouter_and_prompts(rig):
    r = rig(own_pids=(7,), FAKE_LIST="Biorouter -- biorouter [running, pid=7, window=Chat]|gedit -- gedit [running, pid=8, window=Doc]|gcr-prompter -- gcr-prompter [running, pid=9, window=Unlock]")
    listed = text(r.call("list_apps"))
    assert "gedit" in listed and "Biorouter" not in listed.split("]\n", 1)[-1] and "gcr-prompter" not in listed


def test_display_capture_is_refused_and_window_capture_checked(rig):
    r = rig(own_pids=(7,))
    assert "Whole display capture" in text(r.call("screen_capture", {}))
    assert "Whole display capture" in text(r.call("screen_capture", {"display": 0}))
    ok = r.call("screen_capture", {"window_title": "Doc"})
    assert not ok.get("isError") and any(b["type"] == "image" for b in ok["content"])


def test_capture_of_an_own_window_is_refused(rig):
    r = rig(own_pids=(7,), FAKE_CAPTURE_TARGET='{"title": "Biorouter", "app": "Biorouter", "pid": 7}')
    refused = r.call("screen_capture", {"window_title": "Biorouter"})
    assert refused["isError"] and "Biorouter itself" in text(refused)


def test_topology_listing_drops_own_windows(rig):
    r = rig(own_pids=(7,), FAKE_CAPTURE_TARGET='{"title": "Chat", "app": "Biorouter", "pid": 7}')
    listed = text(r.call("screen_capture", {"list_only": True}))
    assert '"pid": 7' not in listed


def test_terminals_are_look_and_click_only(rig):
    r = rig()
    r.call("get_app_state", {"app": "Terminal"})
    assert not r.call("click", {"app": "Terminal", "element_index": "0"}).get("isError")
    r.call("get_app_state", {"app": "Terminal"})
    for name, args in (("type_text", {"text": "ls"}), ("press_key", {"key": "Return"}),
                       ("set_value", {"element_index": "0", "value": "x"})):
        refused = r.call(name, dict(args, app="Terminal"))
        assert refused["isError"] and "terminal" in text(refused)


def test_privacy_settings_are_look_only(rig):
    r = rig(FAKE_TITLE="Privacy & Security")
    looked = r.call("get_app_state", {"app": "System Settings"})
    assert not looked.get("isError")
    refused = r.call("click", {"app": "System Settings", "element_index": "0"})
    assert refused["isError"] and "never changes it" in text(refused)


def test_secrets_and_clipboard_chords_are_refused_before_asking(rig):
    r = rig()
    assert "looks like a secret" in text(r.call("type_text", {"app": "Editor", "text": "ghp_" + "a" * 36}))
    assert "looks like a secret" in text(r.call("set_value", {"app": "Editor", "element_index": "1",
                                                              "value": "AKIAABCDEFGHIJKLMNOP"}))
    assert "Copy, cut and paste" in text(r.call("press_key", {"app": "Editor", "key": "ctrl+v"}))
    assert r.cards == []


def test_look_only_grant_refuses_an_action_after_the_upgrade_is_declined(rig):
    r = rig(answers=(LOOK_ONLY, {"action": "decline"}))
    assert not r.call("get_app_state", {"app": "Editor"}).get("isError")
    refused = r.call("click", {"app": "Editor", "element_index": "0"})
    assert refused["isError"] and "only to look" in text(refused)
    assert len(r.cards) == 2


def test_invalid_arguments_are_refused_before_asking(rig):
    r = rig()
    result = r.call("click", {"app": "Editor", "click_count": 0})
    assert result["isError"] and "Invalid arguments" in text(result)
    assert r.cards == []


def test_browser_sessions_are_refused(rig):
    r = rig(BIOROUTER_SERVE_UI="/usr/share/biorouter/web")
    result = r.call("get_app_state", {"app": "Editor"})
    assert "biorouter serve" in text(result) and r.cards == []


def test_a_missing_desktop_session_is_refused(tmp_path, pass_fake_variables):
    write_payload(tmp_path)
    d = Dispatcher(ask=lambda p, c: ALLOW, environ={"HOME": str(tmp_path)}, root=tmp_path,
                   lock=DesktopLock(tmp_path / "l"), target="linux-x64", identity=StaticIdentity(),
                   question_refusal=lambda: None)
    d.grants.can_ask = True
    result = d.call("get_app_state", {"app": "Editor"}, "c")
    assert "XDG_RUNTIME_DIR" in text(result)


def test_unsupported_computers_get_a_plain_answer(tmp_path):
    d = Dispatcher(environ={}, root=tmp_path, lock=DesktopLock(tmp_path / "l"), target=None, identity=StaticIdentity(),
                   question_refusal=lambda: None)
    assert "does not support this computer" in text(d.call("get_app_state", {"app": "x"}, "c"))
    status = json.loads(text(d.call("setup_status", {}, "c")))
    assert status["supported"] is False


def test_another_chat_gets_a_fresh_helper(rig):
    r = rig(answers=(ALLOW, ALLOW))
    r.call("get_app_state", {"app": "Editor"}, session="a")
    first = r.d.helper.process.pid
    assert "another chat" in text(r.call("get_app_state", {"app": "Editor"}, session="b"))
    r.d.grants.end("a")
    assert r.d.helper is None
    assert not r.call("get_app_state", {"app": "Editor"}, session="b").get("isError")
    assert r.d.helper.process.pid != first
    refused = r.call("click", {"app": "Editor", "element_index": "0"}, session="a")
    assert refused["isError"]


def test_busy_desktop_is_refused(rig, tmp_path):
    r = rig()
    other = DesktopLock(r.lock.path)
    assert other.acquire()
    try:
        result = r.call("get_app_state", {"app": "Editor"})
        assert "Biorouter Copilot is using this computer" in text(result) and r.cards == []
    finally:
        other.release()


def test_setup_tools_need_no_card(rig):
    r = rig()
    status = json.loads(text(r.call("setup_status")))
    assert status["target"] == "linux-x64" and status["helper"]["verified"] is True
    assert "open_permission_setup is for macOS" in text(r.call("open_permission_setup"))
    assert r.cards == []


def test_setup_status_answers_in_json_when_a_check_fails(rig, monkeypatch):
    r = rig()

    def broken():
        raise RuntimeError("a check broke")

    monkeypatch.setattr(r.d, "question_refusal", broken)
    result = r.call("setup_status")
    status = json.loads(text(result))
    assert result["isError"] is True
    assert status["ready"] is False and status["supported"] is False
    assert "a check broke" in status["error"] and status["extension"]["name"] == "Open Computer Use Agent"


def test_setup_status_reports_an_unwritable_executable_in_json(rig, monkeypatch):
    """The live drive's failure: a chmod the OS refuses must come back as setup_status JSON."""
    import os

    from opencomputeruseagent import payload as payload_module

    r = rig()
    os.chmod(r.d.payload.executable, 0o644)

    def refuse(path, mode):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(payload_module.os, "chmod", refuse)
    status = json.loads(text(r.call("setup_status")))
    assert status["ready"] is False and status["helper"] == {"verified": False}
    assert "could not be made executable" in status["message"]


# -- one action at a time, and what a cancel ends -------------------------------------------
def test_an_action_while_another_action_of_the_chat_runs_is_refused(rig):
    r = rig(FAKE_MODE="slow", FAKE_DELAY="1.0", FAKE_SLOW_TOOLS="click")
    r.call("get_app_state", {"app": "Editor"})
    first = r.d.admit("click", "chat-1")
    second = r.d.admit("type_text", "chat-1")
    assert not first.refused and second.refused
    results = {}
    runner = threading.Thread(target=lambda: results.update(
        click=r.call("click", {"app": "Editor", "element_index": "0"}, ticket=first)))
    runner.start()
    refused = r.call("type_text", {"app": "Editor", "text": "x"}, ticket=second)
    runner.join(10)
    assert refused["isError"] and text(refused) == OVERLAP_REFUSAL
    assert not results["click"].get("isError"), text(results["click"])
    # The running action's end lets the next one in.
    assert not r.call("type_text", {"app": "Editor", "text": "x"}).get("isError")


def test_looks_and_other_chats_are_not_held_back_by_a_running_action(rig):
    r = rig()
    held = r.d.admit("click", "chat-1")
    try:
        assert not r.d.admit("get_app_state", "chat-1").refused
        assert not r.d.admit("list_apps", "chat-1").refused
        other = r.d.admit("click", "chat-2")
        assert not other.refused
        r.d.release(other)
    finally:
        r.d.release(held)
    assert not r.d.admit("click", "chat-1").refused


def test_a_cancel_ends_the_chats_access_helper_and_lock(rig):
    r = rig(answers=(ALLOW, ALLOW), FAKE_MODE="honor_cancel", FAKE_DELAY="10", FAKE_SLOW_TOOLS="drag")
    assert not r.call("get_app_state", {"app": "Editor"}, cancel=threading.Event()).get("isError")
    helper = r.d.helper
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    began = time.monotonic()
    result = r.call("drag", {"app": "Editor", "from_x": 1, "from_y": 1, "to_x": 5, "to_y": 5}, cancel=cancel)
    assert time.monotonic() - began < 6
    assert result["isError"] and text(result) == CANCELLED_CALL
    assert r.d.grants.level("chat-1") == 0 and not r.lock.held and r.d.helper is None and not helper.alive
    assert r.d.observed == {}
    assert not r.call("get_app_state", {"app": "Editor"}).get("isError")
    assert len(r.cards) == 2  # the next call asked again


def test_a_call_cancelled_before_it_reaches_the_helper_never_starts(rig, tmp_path):
    log = tmp_path / "fake.log"
    r = rig(FAKE_LOG=str(log))
    r.call("get_app_state", {"app": "Editor"})
    cancel = threading.Event()
    cancel.set()
    result = r.call("click", {"app": "Editor", "element_index": "0"}, cancel=cancel)
    assert result["isError"] and text(result) == CANCELLED_CALL
    calls = [json.loads(line)["message"]["params"]["name"] for line in log.read_text().splitlines()
             if json.loads(line).get("message", {}).get("method") == "tools/call"]
    assert calls == ["get_app_state"]


def test_a_cancelled_call_asks_nothing(rig):
    r = rig()
    cancel = threading.Event()
    cancel.set()
    result = r.call("get_app_state", {"app": "Editor"}, cancel=cancel)
    assert result["isError"] and "cancelled" in text(result) and r.cards == [] and not r.lock.held


# -- the terminal interface that cannot show the card ---------------------------------------
def test_the_terminal_interface_is_refused_before_any_card(rig):
    r = rig(question=host.TUI_REFUSAL)
    result = r.call("get_app_state", {"app": "Editor"})
    assert result["isError"] and text(result) == host.TUI_REFUSAL
    assert r.cards == [] and not r.lock.held and r.d.helper is None
    status = json.loads(text(r.call("setup_status")))
    assert status["question"] == host.TUI_REFUSAL
