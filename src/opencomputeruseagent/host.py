"""Which Biorouter surface started this wrapper, for the one question some surfaces cannot show.

Biorouter's default terminal interface (the full screen TUI of `biorouter session` and of
`biorouter run -s`) cannot show an MCP elicitation: in 1.94.6 it cancels the whole turn the
moment one arrives ("This step needs an interactive form not yet supported in the TUI",
`session/tui/mod.rs`). Sending the consent card there would end the person's turn and the model
would never learn why. So before asking, the wrapper checks where it runs and, when that is the
TUI, refuses with a sentence the model can pass on, and asks nothing.

The CLI picks the TUI exactly when `BIOROUTER_CLI_CLASSIC` is unset and its standard output is a
terminal (`Session::interactive` in `session/mod.rs`). Extensions inherit the CLI's environment
(only daemon private variables are stripped), so the first half is read here directly; the
second is read from the CLI process's own descriptor 1. Anything this cannot tell (Windows, a
process table it cannot read) counts as "the card can be shown", which is the behaviour before
this check existed.
"""
import os
import subprocess
import sys

from . import procs

CLI_NAMES = ("biorouter", "biorouter.exe")
DAEMON_NAMES = ("biorouterd", "biorouterd.exe")
CLASSIC_VARIABLE = "BIOROUTER_CLI_CLASSIC"

TUI_REFUSAL = (
    "Open Computer Use Agent did nothing: it first asks the person in the chat, and Biorouter's "
    "default terminal interface cannot show that question (it would cancel this whole turn). Tell "
    "the person to use the Biorouter desktop app, or to start the terminal with "
    "BIOROUTER_CLI_CLASSIC=1 (for example `BIOROUTER_CLI_CLASSIC=1 biorouter session`)."
)


def _base_name(path):
    return os.path.basename(path or "").lower()


def biorouter_parent(rows=None, pid=None):
    """(kind, pid) of the nearest Biorouter process above this one: ("cli", pid) for the
    terminal `biorouter`, ("daemon", pid) for `biorouterd` (the desktop app and `serve`), or
    (None, None). The nearest one decides: the desktop's own main process sits above biorouterd."""
    rows = procs.table() if rows is None else rows
    executables = {row.pid: row.exe for row in rows}
    chain = procs.ancestry(pid or os.getpid(), rows)
    for ancestor in chain[1:]:
        name = _base_name(executables.get(ancestor))
        if name in DAEMON_NAMES:
            return "daemon", ancestor
        if name in CLI_NAMES:
            return "cli", ancestor
    return None, None


def is_terminal_path(path):
    return bool(path) and (path.startswith("/dev/tty") or path.startswith("/dev/pts/")
                           or path == "/dev/console")


def stdout_is_terminal(pid):
    """Whether process `pid`'s standard output is a terminal, or None when this cannot tell."""
    if sys.platform.startswith("linux"):
        try:
            return is_terminal_path(os.readlink(f"/proc/{pid}/fd/1"))
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/usr/sbin/lsof", "-w", "-a", "-p", str(int(pid)), "-d", "1", "-Fn"],
                                 capture_output=True, text=True, timeout=5,
                                 env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}).stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        names = [line[1:] for line in out.splitlines() if line.startswith("n")]
        return is_terminal_path(names[0]) if names else None
    return None


def question_refusal(environ=None, rows=None, stdout_check=stdout_is_terminal):
    """The refusal to give instead of a consent card this surface cannot show, or None."""
    environ = os.environ if environ is None else environ
    kind, pid = biorouter_parent(rows)
    if kind != "cli" or CLASSIC_VARIABLE in environ:
        return None
    return TUI_REFUSAL if stdout_check(pid) is True else None


class CachedQuestionRefusal:
    """`question_refusal` worked out once: the surface that started a wrapper does not change."""

    def __init__(self, environ=None, compute=question_refusal):
        self.environ, self.compute = environ, compute
        self._done, self._value = False, None

    def __call__(self):
        if not self._done:
            try:
                self._value = self.compute(self.environ)
            except Exception:  # cannot tell: show the card, as before this check
                self._value = None
            self._done = True
        return self._value
