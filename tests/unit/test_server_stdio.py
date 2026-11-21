"""L2 for the wrapper: the real launcher over stdio with a scripted client (fake helper)."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import SESSION_ENV, needs_posix, write_payload  # noqa: E402
from mcp_client import Client, make_install  # noqa: E402
from opencomputeruseagent import consent, payload  # noqa: E402

pytestmark = needs_posix
TARGET = payload.target()
ALLOW = {"action": "accept", "content": {"access": consent.LOOK_AND_ACT}}


@pytest.fixture
def install(tmp_path):
    root = make_install(tmp_path / "install")
    write_payload(root, target=TARGET, fake_env={"FAKE_MODE": "honor_cancel", "FAKE_DELAY": "0.2"})
    return root


@pytest.fixture
def env(tmp_path):
    environ = dict(os.environ)
    environ.update(SESSION_ENV)
    environ["HOME"] = str(tmp_path / "home")  # the desktop lock lives under HOME
    environ.pop("BIOROUTER_SERVE_UI", None)
    return environ


def test_initialize_and_twelve_tools(install, env):
    client = Client(install, env)
    try:
        init = client.initialize
        assert init["protocolVersion"] == "2025-03-26"
        assert init["serverInfo"]["name"] == "opencomputeruseagent" and init["serverInfo"]["version"] == "0.1.0"
        assert init["instructions"].startswith("Open Computer Use Agent is a fallback for desktop control.")
        listed = client.request("tools/list", {})["tools"]
        assert len(listed) == 12
        assert {t["name"]: t["annotations"]["readOnlyHint"] for t in listed}["get_app_state"] is True
        assert client.request("ping", {}) == {}
    finally:
        client.close()


def test_a_card_before_the_first_look_then_none(install, env):
    client = Client(install, env, answer=lambda params: ALLOW)
    try:
        result = client.call("get_app_state", {"app": "Editor"})
        assert not result.get("isError"), client.text(result)
        assert "App=Editor" in client.text(result)
        assert len(client.cards) == 1
        assert client.cards[0]["requestedSchema"]["properties"]["access"]["default"] == consent.DENY
        clicked = client.call("click", {"app": "Editor", "element_index": "0"})
        assert not clicked.get("isError"), client.text(clicked)
        assert len(client.cards) == 1
    finally:
        client.close()


def test_dismissing_the_card_refuses(install, env):
    client = Client(install, env, answer=lambda params: {"action": "cancel"})
    try:
        result = client.call("list_apps")
        assert result["isError"] and "cancelled" in client.text(result)
    finally:
        client.close()


def test_a_client_without_elicitation_is_refused_without_a_card(install, env):
    client = Client(install, env, elicitation=False)
    try:
        result = client.call("get_app_state", {"app": "Editor"})
        assert result["isError"] and "cannot show the question" in client.text(result)
        assert client.cards == []
    finally:
        client.close()


def test_a_call_without_a_chat_id_is_refused(install, env):
    client = Client(install, env, answer=lambda params: ALLOW)
    try:
        result = client.call("list_apps", session=None)
        assert result["isError"] and "chat id" in client.text(result)
    finally:
        client.close()


def test_a_cancelled_call_is_not_answered(install, env):
    slow = {}

    def answer(params):
        time.sleep(1.5)  # the person is slow; the host gives up first
        return ALLOW

    client = Client(install, env, answer=answer)
    try:
        request_id = client.send("tools/call", {"name": "get_app_state", "arguments": {"app": "Editor"},
                                                "_meta": {"biorouter-session-id": "chat-1"}})
        time.sleep(0.3)
        client.notify("notifications/cancelled", {"requestId": request_id, "reason": "timeout"})
        time.sleep(2.5)
        assert not client.answered(request_id)
        assert client.request("ping", {}) == {}
    finally:
        client.close()


def test_a_batch_of_actions_runs_one_and_refuses_the_rest(install, env):
    client = Client(install, env, answer=lambda params: ALLOW)
    try:
        assert not client.call("get_app_state", {"app": "Editor"}).get("isError")
        meta = {"biorouter-session-id": "chat-1"}
        # Biorouter sends a batch's calls at once; the first to arrive runs.
        first = client.send("tools/call", {"name": "click", "arguments": {"app": "Editor", "element_index": "0"},
                                           "_meta": meta})
        second = client.send("tools/call", {"name": "type_text", "arguments": {"app": "Editor", "text": "x"},
                                            "_meta": meta})
        refused, ran = client.wait(second)["result"], client.wait(first)["result"]
        assert refused["isError"] and "Another action in this chat was still running" in client.text(refused)
        assert not ran.get("isError"), client.text(ran)
        assert not client.call("type_text", {"app": "Editor", "text": "x"}).get("isError")
    finally:
        client.close()


def test_a_cancelled_action_ends_the_chats_access(tmp_path, env):
    root = make_install(tmp_path / "slow-install")
    write_payload(root, target=TARGET, fake_env={"FAKE_MODE": "honor_cancel", "FAKE_DELAY": "5",
                                                 "FAKE_SLOW_TOOLS": "click"})
    client = Client(root, env, answer=lambda params: ALLOW)
    try:
        assert not client.call("get_app_state", {"app": "Editor"}).get("isError")
        request_id = client.send("tools/call", {"name": "click", "arguments": {"app": "Editor", "element_index": "0"},
                                                "_meta": {"biorouter-session-id": "chat-1"}})
        time.sleep(0.3)
        client.notify("notifications/cancelled", {"requestId": request_id, "reason": "operation cancelled"})
        time.sleep(1.0)
        assert not client.answered(request_id)
        assert not client.call("get_app_state", {"app": "Editor"}).get("isError")
        assert len(client.cards) == 2  # access ended with the cancel, so the next call asked again
    finally:
        client.close()


def test_a_cancelled_look_ends_access_too(install, env):
    client = Client(install, env, answer=lambda params: ALLOW)
    try:
        assert not client.call("get_app_state", {"app": "Editor"}).get("isError")
        request_id = client.send("tools/call", {"name": "list_apps", "arguments": {},
                                                "_meta": {"biorouter-session-id": "chat-1"}})
        time.sleep(0.15)
        client.notify("notifications/cancelled", {"requestId": request_id, "reason": "operation cancelled"})
        time.sleep(0.6)
        assert not client.answered(request_id)
        assert not client.call("get_app_state", {"app": "Editor"}).get("isError")
        assert len(client.cards) == 2
    finally:
        client.close()
