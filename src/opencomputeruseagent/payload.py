"""Which helper payload this computer runs, whether it is intact, and the helper's environment."""
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, PATCH_REVISION, UPSTREAM_COMMIT, UPSTREAM_VERSION, __version__

TARGETS = ("darwin-arm64", "darwin-x64", "win32-x64", "linux-x64", "linux-arm64")
INSTALL_ROOT = Path(__file__).resolve().parents[2]

UNSUPPORTED = ("Open Computer Use Agent does not support this computer ({detail}). It runs on macOS "
               "14 or later, Windows 10 or 11 on x64, and Linux on X11 (x64 or arm64).")


class PayloadError(Exception):
    pass


def _rosetta():
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], capture_output=True,
                             text=True, timeout=5, env={"PATH": "/usr/sbin:/usr/bin:/bin"}).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "1"


def target(system=None, machine=None, environ=None, rosetta=None):
    """The payload for this computer, or None. On Apple silicon the arm64 helper is chosen even
    when Python itself runs under Rosetta."""
    system = system or _platform.system()
    machine = (machine or _platform.machine() or "").lower()
    environ = os.environ if environ is None else environ
    if system == "Darwin":
        arm = machine in ("arm64", "aarch64") or (rosetta if rosetta is not None else _rosetta())
        return "darwin-arm64" if arm else "darwin-x64"
    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "linux-x64"
        if machine in ("aarch64", "arm64"):
            return "linux-arm64"
        return None
    if system == "Windows":
        arch = " ".join(environ.get(k, "") for k in ("PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432")).upper()
        if "ARM64" in arch:
            return None
        return "win32-x64" if machine in ("amd64", "x86_64") else None
    return None


def describe_platform():
    return f"{_platform.system()} {_platform.release()} {_platform.machine()}".strip()


def executable_for(target_name):
    if target_name.startswith("darwin-"):
        return f"{APP_NAME}.app/Contents/MacOS/ocu"
    return "ocu.exe" if target_name.startswith("win32-") else "ocu"


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Payload:
    """One verified helper payload: `<install>/payload/<target>/<extension version>/`."""

    def __init__(self, target_name, root=INSTALL_ROOT, version=__version__):
        if target_name not in TARGETS:
            raise PayloadError(UNSUPPORTED.format(detail=describe_platform()))
        self.target = target_name
        self.root = Path(root)
        self.version = version
        self.directory = self.root / "payload" / target_name / version
        self.manifest_path = self.directory / "manifest.json"
        self.manifest = None

    @property
    def executable(self):
        return self.directory / executable_for(self.target)

    @property
    def app_bundle(self):
        return self.directory / f"{APP_NAME}.app" if self.target.startswith("darwin-") else None

    def verify(self):
        """Hash every file the payload manifest lists, then restore the executable's mode.

        Biorouter's install writes plain files (no modes, no symlinks), and a reinstall writes
        over the old folder without removing anything, so only listed files are checked and
        stale files elsewhere never stop the wrapper from starting.
        """
        if not self.manifest_path.is_file():
            raise PayloadError(f"The helper for {self.target} is missing from this install. Reinstall "
                               "Open Computer Use Agent.")
        raw = self.manifest_path.read_bytes()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise PayloadError(f"The helper manifest is unreadable: {error}") from None
        expected = {"schema_version": 1, "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
                    "patch_revision": PATCH_REVISION, "target": self.target,
                    "executable": executable_for(self.target), "args": ["mcp"]}
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise PayloadError(f"The helper payload's {key} is {manifest.get(key)!r}, expected {value!r}. "
                                   "Reinstall Open Computer Use Agent.")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise PayloadError("The helper manifest lists no files.")
        listed = set()
        for entry in files:
            relative = entry.get("path", "") if isinstance(entry, dict) else ""
            parts = Path(relative).parts
            if not relative or Path(relative).is_absolute() or ".." in parts:
                raise PayloadError(f"The helper manifest names an unsafe path: {relative!r}")
            path = self.directory / relative
            if path.is_symlink() or not path.is_file():
                raise PayloadError(f"The helper file {relative} is missing. Reinstall Open Computer Use Agent.")
            if _digest(path) != entry.get("sha256"):
                raise PayloadError(f"The helper file {relative} was changed. Reinstall Open Computer Use Agent.")
            listed.add(relative)
        if executable_for(self.target) not in listed:
            raise PayloadError("The helper manifest does not list its executable.")
        if os.name != "nt":
            os.chmod(self.executable, 0o755)
        self.manifest = manifest
        self.manifest_digest = hashlib.sha256(raw).hexdigest()
        return manifest

    def namespace(self):
        """The app agent's socket namespace: one per install and payload."""
        if self.manifest is None:
            self.verify()
        material = f"{self.executable}:{self.manifest_digest}".encode("utf-8")
        return f"ocua-{self.version}-{hashlib.sha256(material).hexdigest()}"

    def stale_version_dirs(self):
        """Folders of other versions an earlier install left behind (installs never delete)."""
        stale = []
        base = self.root / "payload"
        if not base.is_dir():
            return stale
        for target_dir in sorted(base.iterdir()):
            if not target_dir.is_dir() or target_dir.name not in TARGETS:
                continue
            for version_dir in sorted(target_dir.iterdir()):
                if version_dir.is_dir() and version_dir.name != self.version:
                    stale.append(version_dir)
        return stale

    def remove_stale(self):
        removed = []
        for directory in self.stale_version_dirs():
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory)
        return removed


ALWAYS_KEEP = ("HOME", "USER", "LOGNAME", "LANG", "TZ")
LINUX_KEEP = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
              "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS")
WINDOWS_KEEP = ("SystemRoot", "windir", "ComSpec", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "SystemDrive", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                "CommonProgramFiles", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
                "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS")
LINUX_REQUIRED = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def system_path(target_name, environ):
    if target_name.startswith("win32-"):
        root = environ.get("SystemRoot") or environ.get("windir") or r"C:\Windows"
        return ";".join([root + r"\System32", root, root + r"\System32\Wbem",
                         root + r"\System32\WindowsPowerShell\v1.0"])
    if target_name.startswith("linux-"):
        # /usr/bin first: the helper's Python must be the distro's, with PyGObject.
        return "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"
    return "/usr/bin:/bin:/usr/sbin:/sbin"


def helper_environment(target_name, environ, private_dir, namespace):
    """An empty environment plus an allowlist. Biorouter hands every extension the person's own
    variables (provider API keys among them) and its own settings; none of that reaches the
    helper. `OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY` is never passed: it would run the engine
    under Biorouter's identity on macOS."""
    windows = target_name.startswith("win32-")
    keep = list(ALWAYS_KEEP)
    if target_name.startswith("linux-"):
        keep += LINUX_KEEP
    if windows:
        keep += WINDOWS_KEEP
    env = {}
    if windows:
        lowered = {key.lower(): key for key in environ}
        for name in keep:
            source = lowered.get(name.lower())
            if source is not None and environ.get(source):
                env[name] = environ[source]
    else:
        for name in keep:
            if environ.get(name):
                env[name] = environ[name]
        for name, value in environ.items():
            if (name == "LC_ALL" or name.startswith("LC_")) and value:
                env[name] = value
    env["PATH"] = system_path(target_name, environ)
    private = str(private_dir)
    if windows:
        env["TEMP"] = env["TMP"] = private
    else:
        env["TMPDIR"] = private
    env["OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE"] = namespace
    env["OPEN_COMPUTER_USE_CONFIG_FILE"] = os.path.join(private, "no-config", "config.json")
    env["OPEN_COMPUTER_USE_VIRTUAL_DISPLAY_ENABLED"] = "false"
    env["OPEN_COMPUTER_USE_ALLOW_GLOBAL_POINTER_FALLBACKS"] = "1"
    return env


def missing_session_variables(target_name, environ):
    if not target_name.startswith("linux-"):
        return []
    missing = [name for name in LINUX_REQUIRED if not environ.get(name)]
    if not environ.get("DISPLAY") and not environ.get("WAYLAND_DISPLAY"):
        missing.append("DISPLAY")
    return missing


def is_serve_daemon(environ=None):
    """`biorouter serve` (a browser): the consent card needs the user action proof, which a
    keyless daemon never holds, so the fallback cannot ask and refuses there."""
    environ = os.environ if environ is None else environ
    value = environ.get("BIOROUTER_SERVE_UI", "").strip().lower()
    return bool(value) and value not in ("0", "false", "no", "off")


def current_platform():
    return sys.platform
