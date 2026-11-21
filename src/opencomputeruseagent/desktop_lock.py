"""The one-desktop-owner lock shared with Biorouter Copilot.

Biorouter Copilot holds an exclusive lock on `<local data folder>/BioRouter/computer-use.desktop.lock`
for the life of a grant (`desktop_lock_path` in Biorouter's `security/computer_use.rs`, through the
fs2 crate: `flock` on Unix, `LockFileEx` over the whole file on Windows). The fallback takes the
same file the same way for the life of its own grant, so the two never drive one desktop at once.
The path is a soft contract with Biorouter's core; the nightly workflow checks it against the
newest release.
"""
import os
import sys
from pathlib import Path

BUSY_REFUSAL = ("Biorouter Copilot is using this computer in another chat. Stop that task first, "
                "then try again.")


def data_local_dir(environ=None, platform=None):
    """Rust's `dirs::data_local_dir()`, which Biorouter uses for the lock."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = environ.get("HOME") or os.path.expanduser("~")
    if platform == "darwin":
        return Path(home) / "Library" / "Application Support"
    if platform == "win32":
        local = environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path(environ.get("USERPROFILE") or home) / "AppData" / "Local"
    xdg = environ.get("XDG_DATA_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg)
    return Path(home) / ".local" / "share"


def lock_path(environ=None, platform=None):
    return data_local_dir(environ, platform) / "BioRouter" / "computer-use.desktop.lock"


class DesktopLock:
    def __init__(self, path=None):
        self.path = Path(path) if path else lock_path()
        self._handle = None

    @property
    def held(self):
        return self._handle is not None

    def acquire(self):
        """Take the lock without waiting. True when held (already or now)."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
