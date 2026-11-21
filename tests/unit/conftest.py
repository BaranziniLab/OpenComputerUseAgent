import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from opencomputeruseagent import PATCH_REVISION, UPSTREAM_COMMIT, UPSTREAM_VERSION, __version__  # noqa: E402
from opencomputeruseagent.payload import executable_for  # noqa: E402

FAKE_HELPER = Path(__file__).with_name("fake_helper.py")
SESSION_ENV = {"HOME": "/home/tester", "XDG_RUNTIME_DIR": "/run/user/1000", "DISPLAY": ":0",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}


def write_payload(root, target="linux-x64", script=None, version=__version__, extra=None, fake_env=None):
    """A payload folder laid out as an installed .brxt has it, with a real manifest.

    `fake_env` is baked into the fake helper, for tests that run the wrapper as a subprocess
    (its environment allowlist drops FAKE_* variables)."""
    directory = Path(root) / "payload" / target / version
    executable = directory / executable_for(target)
    executable.parent.mkdir(parents=True, exist_ok=True)
    if script is None:
        script = (f"#!{sys.executable}\nimport os, runpy, sys\nos.environ.update({dict(fake_env or {})!r})\n"
                  f"sys.argv[0] = {str(FAKE_HELPER)!r}\nrunpy.run_path({str(FAKE_HELPER)!r}, run_name='__main__')\n")
    executable.write_text(script)
    executable.chmod(0o644)
    (directory / "LICENSE").write_text("MIT\n")
    for name, text in (extra or {}).items():
        (directory / name).write_text(text)
    files = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        files.append({"path": path.relative_to(directory).as_posix(),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"schema_version": 1, "upstream_commit": UPSTREAM_COMMIT, "upstream_version": UPSTREAM_VERSION,
                "patch_revision": PATCH_REVISION, "target": target, "executable": executable_for(target),
                "args": ["mcp"], "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


@pytest.fixture
def install(tmp_path):
    write_payload(tmp_path)
    return tmp_path


@pytest.fixture
def session_env(tmp_path):
    return dict(SESSION_ENV, HOME=str(tmp_path))


needs_posix = pytest.mark.skipif(os.name == "nt", reason="the fake helper is a POSIX script")


@pytest.fixture
def pass_fake_variables(monkeypatch):
    """FAKE_* are test controls; the wrapper's allowlist drops them, so let them through here."""
    from opencomputeruseagent import payload

    real = payload.helper_environment

    def with_fake(target, environ, private, namespace):
        env = real(target, environ, private, namespace)
        env.update({k: v for k, v in environ.items() if k.startswith("FAKE_")})
        return env

    monkeypatch.setattr(payload, "helper_environment", with_fake)
