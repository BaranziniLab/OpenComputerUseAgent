"""What the fallback refuses before anything reaches the helper or the model.

These are the built-in Biorouter Copilot's rules (its `guard.rs` lists and refusals), copied so
the fallback is never the looser of the two. They are accident prevention around a cooperative
model and injected instructions, not a boundary against a local program: the person's own
shell can always run what it likes.
"""
import os
import re
import sys
import time
from collections import namedtuple
from pathlib import Path

from . import procs
from . import BUNDLE_IDENTIFIER

Facts = namedtuple("Facts", "name bundle_id pid")

OWN_REFUSAL = (
    "That is Biorouter itself, or this extension's own helper. Open Computer Use Agent never looks "
    "at or controls those windows, because they show other chats and Biorouter's own approval "
    "controls. For a web page use the browser tools where this chat offers them; otherwise ask "
    "the person."
)
SECURITY_PROMPT_REFUSAL = (
    "That is a system security or password prompt, a password store or a notification centre. "
    "Open Computer Use Agent never looks at or controls those; ask the person to answer it "
    "themselves."
)
SECURE_INPUT_REFUSAL = (
    "A password field somewhere on this computer holds the keyboard right now (secure keyboard "
    "input), so Open Computer Use Agent will not send keys or text. Ask the person to finish or "
    "close it."
)
TERMINAL_REFUSAL = (
    "That app is a terminal. Typing, keys, drops or menu actions there would run commands outside "
    "Biorouter's own shell tools, their permission mode and folder access, so Open Computer Use "
    "Agent only looks, clicks and scrolls there. Use the shell tools for commands, or ask the "
    "person."
)
PRIVACY_SETTINGS_REFUSAL = (
    "That is a privacy, security, users or sign-in settings page. Open Computer Use Agent can look "
    "at it but never changes it; tell the person what to change."
)
CLIPBOARD_REFUSAL = (
    "Copy, cut and paste chords are refused: they read or replace the person's clipboard. Type the "
    "text with type_text, or set it with set_value."
)
DISPLAY_CAPTURE_REFUSAL = (
    "Whole display capture is refused, because a display shows every app at once, Biorouter's own "
    "windows included. Capture one app window with window_title, or use get_app_state."
)

# The desktop app's and Biorouter Copilot's own bundle identifiers (and their helpers'), plus
# this extension's own helper app.
OWN_BUNDLES = ("com.electron.biorouter", "edu.ucsf.biorouter", "org.biorouter.computer-use", BUNDLE_IDENTIFIER)
OWN_NAMES = ("biorouter", "biorouterd", "open computer use agent", "biorouter computer use")

SECURITY_NAMES = (
    "securityagent", "coreautha", "coreauthd", "usernotificationcenter", "loginwindow",
    "screensaverengine", "keychain access", "passwords",
    "consent", "credentialuibroker", "logonui", "lockapp",
    "polkit-gnome-authentication-agent-1", "polkit-kde-authentication-agent-1",
    "polkit-mate-authentication-agent-1", "lxpolkit", "lxqt-policykit-agent", "xfce-polkit",
    "gcr-prompter", "gcr-viewer", "ssh-askpass", "ksshaskpass", "seahorse",
    # Notification centres: what they show is other apps' messages.
    "notificationcenter", "notification center", "notification centre",
)
SECURITY_BUNDLES = (
    "com.apple.securityagent", "com.apple.loginwindow", "com.apple.usernotificationcenter",
    "com.apple.screensaver.engine", "com.apple.keychainaccess", "com.apple.passwords",
    "org.gnome.seahorse", "com.apple.notificationcenterui",
)
TERMINAL_NAMES = (
    "terminal", "iterm2", "iterm", "warp", "ghostty", "kitty", "alacritty", "wezterm", "wezterm-gui",
    "hyper", "tabby", "rio", "windowsterminal", "openconsole", "conhost", "cmd", "powershell", "pwsh",
    "powershell_ise", "wt", "mintty", "putty", "gnome-terminal", "gnome-terminal-server", "konsole",
    "xterm", "uxterm", "xfce4-terminal", "tilix", "terminator", "foot", "urxvt", "rxvt", "lxterminal",
    "mate-terminal", "qterminal", "sakura", "terminology", "guake", "yakuake", "ptyxis", "kgx",
    "blackbox", "warp-terminal", "cool-retro-term", "st",
)
TERMINAL_BUNDLES = (
    "com.apple.terminal", "com.googlecode.iterm2", "dev.warp.warp-stable", "dev.warp.warp",
    "com.mitchellh.ghostty", "net.kovidgoyal.kitty", "org.alacritty", "io.alacritty",
    "com.github.wez.wezterm", "co.zeit.hyper", "org.tabby", "com.raphaelamorim.rio",
    "org.gnome.terminal", "org.gnome.console", "org.gnome.ptyxis", "org.kde.konsole",
    "com.gexperts.tilix", "org.wezfurlong.wezterm",
)
SETTINGS_NAMES = (
    "system settings", "system preferences", "systemsettings", "settings", "applicationframehost",
    "gnome-control-center", "systemsettings5", "sechealthui",
)
SETTINGS_BUNDLES = (
    "com.apple.systempreferences", "com.apple.settings", "org.gnome.settings", "org.kde.systemsettings",
    "windows.immersivecontrolpanel",
)
PRIVACY_PANES = (
    "privacy", "security", "users", "login", "sign-in", "sign in", "password", "touch id",
    "lock screen", "accounts", "family", "device management", "profiles", "online accounts", "sharing",
)

# The tools that send keys, text, drops or menu actions: refused in terminals.
KEYBOARD_LIKE = ("type_text", "press_key", "set_value", "drag", "perform_secondary_action")


def _lower(value):
    return (value or "").strip().lower()


def _base_name(name):
    lowered = _lower(name)
    return lowered[:-4] if lowered.endswith(".exe") else lowered


def _matches(facts, names, bundles):
    name = _base_name(facts.name)
    bundle = _lower(facts.bundle_id)
    if name and name in names:
        return True
    return bool(bundle) and any(bundle == b or bundle.startswith(b + ".") for b in bundles)


def is_security_prompt(facts):
    return _matches(facts, SECURITY_NAMES, SECURITY_BUNDLES) or _base_name(facts.name).startswith("pinentry")


def is_terminal(facts):
    return _matches(facts, TERMINAL_NAMES, TERMINAL_BUNDLES)


def is_settings_app(facts):
    return _matches(facts, SETTINGS_NAMES, SETTINGS_BUNDLES)


def is_privacy_settings(facts, window_title):
    if _base_name(facts.name) == "sechealthui":
        return True
    if not is_settings_app(facts):
        return False
    title = _lower(window_title)
    # An unknown title in a settings app counts as a privacy pane: refusing is the safe answer.
    return not title or any(pane in title for pane in PRIVACY_PANES)


def is_own_bundle(facts):
    return _matches(facts, (), OWN_BUNDLES)


def query_names_protected(query):
    """A query string that already names something refused, before the helper resolves it."""
    facts = Facts(query, query, 0)
    lowered = _lower(query)
    return (lowered in OWN_NAMES or is_own_bundle(facts) or is_security_prompt(facts)
            or lowered.startswith("biorouter"))


def install_root(executable):
    """The running app's install folder: the outermost enclosing `.app` on macOS, or the folder
    holding `resources/bin/` on Windows and Linux desktop installs. A CLI in a shared folder such
    as `/usr/bin` has none, so the rule never claims unrelated programs beside it."""
    path = Path(executable)
    apps = [p for p in [path] + list(path.parents) if p.suffix.lower() == ".app"]
    if apps:
        return apps[-1]
    bin_dir = path.parent
    resources = bin_dir.parent
    if bin_dir.name.lower() == "bin" and resources.name.lower() == "resources":
        return resources.parent
    return None


def _norm(path):
    """A path compared the way the OS compares them: on Windows, one separator and no case."""
    path = str(path or "")
    if os.name == "nt":
        return path.replace("\\", "/").lower()
    return path


class Identity:
    """Which processes are Biorouter's own: this wrapper's ancestry (uv, biorouterd, the desktop
    app or the terminal), every descendant of the desktop main process, every process whose
    executable lies inside the running Biorouter's install folder, and this extension's helper."""

    def __init__(self, pids=(), roots=(), executables=None, helper_paths=()):
        self.pids = set(pids)
        self.roots = [str(r) for r in roots if r]
        self.executables = dict(executables or {})
        self.helper_paths = [str(p) for p in helper_paths if p]

    @classmethod
    def current(cls, helper_paths=(), rows=None):
        rows = procs.table() if rows is None else rows
        chain = procs.ancestry(os.getpid(), rows)
        pids = set(chain)
        executables = {row.pid: row.exe for row in rows if row.exe}
        main = os.environ.get("BIOROUTER_DESKTOP_MAIN_PID", "").strip()
        if main.isdigit() and int(main) > 1 and any(row.pid == int(main) for row in rows):
            pids.add(int(main))
            pids |= procs.descendants(int(main), rows)
        roots = []
        for pid in chain:
            exe = executables.get(pid, "")
            if os.path.basename(exe).lower() in ("biorouterd", "biorouterd.exe", "biorouter", "biorouter.exe"):
                root = install_root(exe)
                if root:
                    roots.append(root)
        return cls(pids, roots, executables, helper_paths)

    def owns_pid(self, pid):
        if not pid:
            return False
        if pid in self.pids or pid == os.getpid():
            return True
        exe = self.executables.get(pid, "")
        if not exe:
            return False
        norm = _norm(exe)
        for root in self.roots:
            base = _norm(root).rstrip("/")
            if norm == base or norm.startswith(base + "/"):
                return True
        return any(norm.startswith(_norm(path)) for path in self.helper_paths)

    def owns(self, facts):
        return self.owns_pid(facts.pid) or is_own_bundle(facts) or _base_name(facts.name) in OWN_NAMES


class CachedIdentity:
    def __init__(self, helper_paths=(), ttl=10.0, clock=time.monotonic):
        self.helper_paths, self.ttl, self.clock = list(helper_paths), ttl, clock
        self._value, self._at = None, None

    def get(self):
        now = self.clock()
        if self._value is None or now - self._at > self.ttl:
            self._value, self._at = Identity.current(self.helper_paths), now
        return self._value


# Typed credentials (the built-in's refuse_secret_text uses Biorouter's output scanner; this is
# the same family of shapes).
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("AWS secret", re.compile(r"(?i)\b(?:aws_?)?secret_?access_?key\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+]{40}")),
    ("API token", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("API token", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}")),
    ("API token", re.compile(r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b")),
    ("API token", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("API token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("API token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("API token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("API token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
]


def secret_kinds(text):
    kinds = []
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(text or "") and kind not in kinds:
            kinds.append(kind)
    return kinds


def secret_refusal(text):
    kinds = secret_kinds(text)
    if not kinds:
        return None
    return (f"This text looks like a secret ({', '.join(kinds)}), so Open Computer Use Agent will not "
            "type it into an app. Ask the person to enter it themselves.")


PRIMARY_MODIFIERS = {"cmd", "command", "ctrl", "control", "super", "meta", "win", "windows", "primary", "mod"}


def is_clipboard_chord(key):
    parts = [p.strip().lower() for p in re.split(r"[+\-](?=.)", (key or "").replace(" ", "")) if p.strip()]
    if len(parts) < 2:
        return False
    *modifiers, last = parts
    modifiers = set(modifiers)
    if modifiers & PRIMARY_MODIFIERS and last in ("c", "x", "v"):
        return True
    if last in ("insert", "ins") and modifiers & ({"shift"} | PRIMARY_MODIFIERS):
        return True
    return last in ("delete", "del") and "shift" in modifiers


def secure_input_enabled():
    """macOS: whether some app's password field holds the keyboard (secure event input).

    `IsSecureEventInputEnabled` is a Carbon call that needs no permission. It is the only
    native call the wrapper makes; everything that needs Accessibility or Screen Recording runs
    in the helper app, which holds those grants (tests/unit/test_tcc_free.py checks this file).
    """
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        function = carbon.IsSecureEventInputEnabled
        function.restype = ctypes.c_ubyte
        return bool(function())
    except (OSError, AttributeError):
        return False


HEADER = re.compile(r"^App=(?P<app>.*) \(pid (?P<pid>\d+)\)\s*$")
WINDOW = re.compile(r'^Window: "(?P<title>.*)", App: (?P<name>.*?)\.?\s*$')


def facts_from_state(text):
    """(Facts, window title) from a get_app_state or action result, or (None, None)."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines[:6]):
        match = HEADER.match(line)
        if not match:
            continue
        reference, pid = match.group("app"), int(match.group("pid"))
        name, title = reference, None
        if index + 1 < len(lines):
            window = WINDOW.match(lines[index + 1])
            if window:
                name, title = window.group("name"), window.group("title")
        bundle = reference if "." in reference and " " not in reference and reference != name else None
        return Facts(name, bundle, pid), title
    return None, None


LIST_LINE = re.compile(r"^(?P<name>.*?) (?:—|--) (?P<id>\S+) \[(?P<markers>.*)\]\s*$")


def facts_from_list_line(line):
    match = LIST_LINE.match(line.strip())
    if not match:
        return None
    pid_match = re.search(r"pid=(\d+)", match.group("markers"))
    identifier = match.group("id")
    bundle = identifier if "." in identifier else None
    return Facts(match.group("name"), bundle, int(pid_match.group(1)) if pid_match else 0)
