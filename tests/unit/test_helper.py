import json
import os
import threading
import time

import pytest

from conftest import SESSION_ENV, needs_posix, write_payload
from opencomputeruseagent import __version__
from opencomputeruseagent import helper as helper_module
from opencomputeruseagent.helper import Cancelled, Helper, HelperError
from opencomputeruseagent.payload import Payload

pytestmark = needs_posix


def start(tmp_path, mode="normal", **env):
    write_payload(tmp_path)
    environ = dict(SESSION_ENV, FAKE_MODE=mode, OPENAI_API_KEY="sk-secret", BIOROUTER_PORT="1", **env)
    return Helper(Payload("linux-x64", tmp_path), environ=environ)


@pytest.fixture(autouse=True)
def _fake_variables(pass_fake_variables):
    pass


def test_handshake_and_a_call(tmp_path):
    log = tmp_path / "fake.log"
    h = start(tmp_path, FAKE_LOG=str(log)).start()
    try:
        assert h.server_info["version"] == "1.1.5"
        result = h.call_tool("get_app_state", {"app": "Editor"})
        assert "App=Editor (pid 4242)" in result["content"][0]["text"]
    finally:
        h.stop()
    env = json.loads(log.read_text().splitlines()[0])["env"]
    assert "OPENAI_API_KEY" not in env and "BIOROUTER_PORT" not in env
    assert env["OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE"].startswith(f"ocua-{__version__}-")
    assert not h.alive


def test_version_mismatch_refuses_to_start(tmp_path):
    with pytest.raises(HelperError, match="version"):
        start(tmp_path, "wrong_version").start()


def test_schema_mismatch_refuses_to_start(tmp_path):
    with pytest.raises(HelperError, match="pinned contract"):
        start(tmp_path, "wrong_schema").start()


def test_a_timeout_ends_the_helper_and_says_the_outcome_is_uncertain(tmp_path):
    h = start(tmp_path, "stuck").start()
    began = time.monotonic()
    with pytest.raises(HelperError, match="uncertain"):
        h.call_tool("click", {"app": "Editor"}, timeout=1.0)
    assert time.monotonic() - began < 8
    assert not h.alive


def test_a_cancel_the_helper_honours_keeps_it(tmp_path):
    h = start(tmp_path, "honor_cancel", FAKE_DELAY="10").start()
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    try:
        with pytest.raises(Cancelled):
            h.call_tool("drag", {"app": "Editor"}, cancel=cancel)
        assert h.alive
        assert h.request("ping", {}, timeout=5) == {}
    finally:
        h.stop()


def test_a_cancel_the_helper_cannot_settle_ends_it(tmp_path, monkeypatch):
    monkeypatch.setattr(helper_module, "CANCEL_GRACE", 0.5)
    h = start(tmp_path, "stuck").start()
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(Cancelled):
        h.call_tool("drag", {"app": "Editor"}, cancel=cancel)
    assert not h.alive


def test_a_crashed_helper_fails_the_call(tmp_path):
    h = start(tmp_path, "slow", FAKE_DELAY="10").start()
    threading.Timer(0.3, lambda: os.kill(h.process.pid, 9)).start()
    with pytest.raises(HelperError, match="uncertain"):
        h.call_tool("click", {"app": "Editor"})
