"""Which macOS app agents the wrapper ends: exactly this install's app, parent launchd."""
import os
import signal
import sys

from opencomputeruseagent import APP_NAME, dispatch
from opencomputeruseagent.procs import Process


def agent_path(directory):
    return os.path.join(str(directory), f"{APP_NAME}.app", "Contents", "MacOS", "ocu")


def test_the_agent_is_found_by_its_real_path(tmp_path, monkeypatch):
    real = tmp_path / "real" / "payload"
    os.makedirs(os.path.dirname(agent_path(real)))
    open(agent_path(real), "w").close()
    link = tmp_path / "link"
    os.symlink(tmp_path / "real", link)
    killed = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dispatch.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rows = [Process(10, 1, os.path.realpath(agent_path(real))),   # LaunchServices reports the real path
            Process(11, 500, os.path.realpath(agent_path(real))),  # a proxy, not the agent
            Process(12, 1, "/Applications/Other.app/Contents/MacOS/ocu"),
            Process(13, 1, "/usr/bin/ocu-not")]
    assert dispatch.end_app_agents([link / "payload"], rows) == [10]
    assert killed == [(10, signal.SIGTERM)]


def test_a_proxy_of_this_install_is_seen_through_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)

    class FakePayload:
        executable = link / "ocu"

    (real / "ocu").write_text("")
    assert dispatch.other_proxies_running(FakePayload, [Process(20, 400, str(real / "ocu"))])
    assert not dispatch.other_proxies_running(FakePayload, [Process(20, 1, str(real / "ocu"))])
