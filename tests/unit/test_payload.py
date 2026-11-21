import os
import stat

import pytest

from conftest import write_payload
from opencomputeruseagent import payload as payload_module
from opencomputeruseagent.payload import Payload, PayloadError, helper_environment, target


@pytest.mark.parametrize("system, machine, rosetta, expected", [
    ("Darwin", "arm64", False, "darwin-arm64"),
    ("Darwin", "x86_64", True, "darwin-arm64"),   # Python under Rosetta on Apple silicon
    ("Darwin", "x86_64", False, "darwin-x64"),
    ("Linux", "x86_64", False, "linux-x64"),
    ("Linux", "aarch64", False, "linux-arm64"),
    ("Linux", "riscv64", False, None),
    ("Windows", "AMD64", False, "win32-x64"),
    ("FreeBSD", "amd64", False, None),
])
def test_payload_pick(system, machine, rosetta, expected):
    assert target(system, machine, {}, rosetta) == expected


def test_windows_on_arm_is_not_offered_the_x64_helper():
    assert target("Windows", "AMD64", {"PROCESSOR_ARCHITEW6432": "ARM64"}) is None
    assert target("Windows", "ARM64", {"PROCESSOR_ARCHITECTURE": "ARM64"}) is None


def test_verify_restores_mode_only_on_the_executable(tmp_path):
    directory = write_payload(tmp_path)
    p = Payload("linux-x64", tmp_path)
    p.verify()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p.executable).st_mode) == 0o755
        assert stat.S_IMODE(os.stat(directory / "LICENSE").st_mode) & 0o111 == 0


def test_a_changed_byte_refuses_to_start(tmp_path):
    directory = write_payload(tmp_path)
    (directory / "LICENSE").write_text("MIT!\n")
    with pytest.raises(PayloadError, match="was changed"):
        Payload("linux-x64", tmp_path).verify()


def test_a_missing_file_refuses_to_start(tmp_path):
    directory = write_payload(tmp_path)
    (directory / "LICENSE").unlink()
    with pytest.raises(PayloadError, match="missing"):
        Payload("linux-x64", tmp_path).verify()


def test_stale_files_the_manifest_does_not_list_do_not_block(tmp_path):
    directory = write_payload(tmp_path)
    (directory / "leftover-from-an-old-install").write_text("x")
    Payload("linux-x64", tmp_path).verify()


def test_a_foreign_pin_is_refused(tmp_path):
    import json

    directory = write_payload(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["patch_revision"] = "biorouter-4"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(PayloadError, match="patch_revision"):
        Payload("linux-x64", tmp_path).verify()


def test_unsafe_manifest_paths_are_refused(tmp_path):
    import json

    directory = write_payload(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["files"].append({"path": "../../etc/passwd", "sha256": "0"})
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(PayloadError, match="unsafe"):
        Payload("linux-x64", tmp_path).verify()


def test_namespace_is_per_install_and_payload(tmp_path):
    write_payload(tmp_path / "a")
    write_payload(tmp_path / "b")
    a, b = Payload("linux-x64", tmp_path / "a"), Payload("linux-x64", tmp_path / "b")
    na, nb = a.namespace(), b.namespace()
    assert na.startswith("ocua-0.1.0-") and len(na) == len("ocua-0.1.0-") + 64
    assert na != nb
    assert a.namespace() == na


def test_stale_version_folders_are_found_and_removed(tmp_path):
    write_payload(tmp_path)
    write_payload(tmp_path, version="0.0.9")
    write_payload(tmp_path, target="darwin-arm64", version="0.0.9")
    p = Payload("linux-x64", tmp_path)
    stale = p.stale_version_dirs()
    assert sorted(d.name for d in stale) == ["0.0.9", "0.0.9"]
    p.remove_stale()
    assert p.stale_version_dirs() == []
    p.verify()


DIRTY = {
    "HOME": "/home/me", "USER": "me", "LANG": "en_US.UTF-8", "LC_CTYPE": "C.UTF-8", "TZ": "UTC",
    "PATH": "/ext/.venv/bin:/usr/local/bin:/usr/bin", "VIRTUAL_ENV": "/ext/.venv", "UV": "/usr/bin/uv",
    "OPENAI_API_KEY": "sk-secret", "SPIKE_SENTINEL_API_KEY": "x", "ANTHROPIC_API_KEY": "y",
    "BIOROUTER_PORT": "1234", "BIOROUTER_PATH_ROOT": "/tmp/root", "BIOROUTER_SERVER__SECRET_KEY": "k",
    "OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY": "1", "OPEN_COMPUTER_USE_CONFIG_FILE": "/home/me/.config/ocu.json",
    "DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b",
    "AT_SPI_BUS_ADDRESS": "unix:path=/a", "SSH_AUTH_SOCK": "/tmp/agent",
}


@pytest.mark.parametrize("target_name", ["linux-x64", "darwin-arm64", "win32-x64"])
def test_environment_allowlist(target_name, tmp_path):
    env = helper_environment(target_name, DIRTY, tmp_path, "ocua-0.1.0-abc")
    assert not [k for k in env if k.startswith("BIOROUTER_")]
    assert not [k for k in env if k.endswith("_API_KEY")]
    assert "VIRTUAL_ENV" not in env and "UV" not in env and "SSH_AUTH_SOCK" not in env
    assert ".venv" not in env["PATH"]
    assert "OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY" not in env
    assert env["OPEN_COMPUTER_USE_CONFIG_FILE"].startswith(str(tmp_path))
    assert not os.path.exists(env["OPEN_COMPUTER_USE_CONFIG_FILE"])
    assert env["OPEN_COMPUTER_USE_VIRTUAL_DISPLAY_ENABLED"] == "false"
    assert env["OPEN_COMPUTER_USE_ALLOW_GLOBAL_POINTER_FALLBACKS"] == "1"
    assert env["OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE"] == "ocua-0.1.0-abc"
    if target_name.startswith("linux"):
        assert env["PATH"].split(":")[0] == "/usr/bin"
        assert env["DISPLAY"] == ":1" and env["XDG_RUNTIME_DIR"] == "/run/user/1"
        assert env["DBUS_SESSION_BUS_ADDRESS"] and env["AT_SPI_BUS_ADDRESS"]
        assert env["LC_CTYPE"] == "C.UTF-8" and env["TMPDIR"] == str(tmp_path)
    if target_name.startswith("darwin"):
        assert "DISPLAY" not in env and "DBUS_SESSION_BUS_ADDRESS" not in env
    if target_name.startswith("win32"):
        assert env["TEMP"] == env["TMP"] == str(tmp_path)


def test_windows_keeps_system_variables_case_insensitively(tmp_path):
    env = helper_environment("win32-x64", {"SYSTEMROOT": r"C:\Windows", "LocalAppData": r"C:\U\L",
                                           "OPENAI_API_KEY": "x"}, tmp_path, "n")
    assert env["SystemRoot"] == r"C:\Windows" and env["LOCALAPPDATA"] == r"C:\U\L"
    assert env["PATH"].startswith(r"C:\Windows\System32;")


def test_linux_session_variables_are_required():
    assert payload_module.missing_session_variables("linux-x64", {}) == ["XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY"]
    assert payload_module.missing_session_variables("linux-x64", {"XDG_RUNTIME_DIR": "a", "DBUS_SESSION_BUS_ADDRESS": "b", "WAYLAND_DISPLAY": "w"}) == []
    assert payload_module.missing_session_variables("darwin-arm64", {}) == []


def test_serve_daemon_detection():
    assert payload_module.is_serve_daemon({"BIOROUTER_SERVE_UI": "/usr/share/biorouter/web"})
    assert not payload_module.is_serve_daemon({"BIOROUTER_SERVE_UI": ""})
    assert not payload_module.is_serve_daemon({})
