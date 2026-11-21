import os
import subprocess
import sys
from pathlib import Path

from opencomputeruseagent.desktop_lock import DesktopLock, lock_path

ROOT = Path(__file__).resolve().parents[2]


def test_lock_path_matches_biorouter_copilots():
    assert lock_path({"HOME": "/Users/me"}, "darwin") == Path("/Users/me/Library/Application Support/BioRouter/computer-use.desktop.lock")
    assert lock_path({"HOME": "/home/me"}, "linux") == Path("/home/me/.local/share/BioRouter/computer-use.desktop.lock")
    assert lock_path({"HOME": "/home/me", "XDG_DATA_HOME": "/data"}, "linux") == Path("/data/BioRouter/computer-use.desktop.lock")
    assert lock_path({"HOME": "/home/me", "XDG_DATA_HOME": "relative"}, "linux") == Path("/home/me/.local/share/BioRouter/computer-use.desktop.lock")
    assert lock_path({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, "win32") == Path(r"C:\Users\me\AppData\Local") / "BioRouter" / "computer-use.desktop.lock"


def test_exclusive_between_holders(tmp_path):
    path = tmp_path / "BioRouter" / "computer-use.desktop.lock"
    first, second = DesktopLock(path), DesktopLock(path)
    assert first.acquire() and first.held
    assert first.acquire()  # already held
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_exclusive_across_processes(tmp_path):
    path = tmp_path / "computer-use.desktop.lock"
    holder = DesktopLock(path)
    assert holder.acquire()
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from opencomputeruseagent.desktop_lock import DesktopLock\n"
            "print(DesktopLock(%r).acquire())") % (str(ROOT / "src"), str(path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == "False"
    holder.release()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == "True"
