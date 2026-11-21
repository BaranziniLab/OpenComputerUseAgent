"""Where the wrapper runs decides whether the consent card can be shown at all (host.py)."""
import os
import subprocess
import sys
import time

import pytest

from opencomputeruseagent import consent, host
from opencomputeruseagent.consent import LOOK, Grants, Refusal
from opencomputeruseagent.procs import Process

ME = os.getpid()


def chain(*exes):
    """A process table: this process, then its parents named by `exes`, nearest first."""
    rows, pid = [], ME
    parents = list(range(9000, 9000 + len(exes)))
    for exe, parent in zip(("/usr/bin/python3",) + exes, parents + [1]):
        rows.append(Process(pid, parent, exe))
        pid = parent
    return rows, parents


def test_the_terminal_interface_is_found_and_refused_without_a_card():
    rows, parents = chain("/home/me/.local/share/uv/uv", "/home/me/.local/bin/biorouter", "/usr/bin/zsh")
    seen = []

    def terminal(pid):
        seen.append(pid)
        return True

    assert host.question_refusal({}, rows, terminal) == host.TUI_REFUSAL
    assert seen == [parents[1]]  # the CLI's own standard output was read


def test_the_classic_terminal_shows_the_card():
    rows, _ = chain("/usr/bin/uv", "/usr/local/bin/biorouter")
    assert host.question_refusal({"BIOROUTER_CLI_CLASSIC": "1"}, rows, lambda pid: True) is None
    # The CLI reads it with `is_ok()`: set to anything, even empty, means classic.
    assert host.question_refusal({"BIOROUTER_CLI_CLASSIC": ""}, rows, lambda pid: True) is None


def test_a_cli_whose_output_is_not_a_terminal_is_classic_and_shows_the_card():
    rows, _ = chain("/usr/bin/uv", "/usr/local/bin/biorouter")
    assert host.question_refusal({}, rows, lambda pid: False) is None


def test_what_cannot_be_told_shows_the_card_as_before():
    rows, _ = chain("/usr/bin/uv", "/usr/local/bin/biorouter")
    assert host.question_refusal({}, rows, lambda pid: None) is None
    assert host.question_refusal({}, [], lambda pid: True) is None


def test_the_daemon_is_the_nearest_biorouter_under_the_desktop_app():
    # The desktop main process can be called biorouter too (Linux packages): biorouterd is nearer.
    rows, _ = chain("/usr/bin/uv", "/usr/lib/biorouter/resources/bin/biorouterd", "/usr/lib/biorouter/biorouter")
    assert host.biorouter_parent(rows)[0] == "daemon"
    assert host.question_refusal({}, rows, lambda pid: True) is None


def test_windows_names_are_recognised():
    rows, parents = chain(r"C:\Users\me\.local\bin\uv.exe", "biorouter.exe")
    assert host.biorouter_parent(rows) == ("cli", parents[1])


def test_no_biorouter_above_shows_the_card():
    rows, _ = chain("/usr/bin/uv", "/usr/bin/bash")
    assert host.biorouter_parent(rows) == (None, None)


@pytest.mark.parametrize("path,expected", [
    ("/dev/ttys003", True), ("/dev/pts/4", True), ("/dev/tty", True), ("/dev/console", True),
    ("/dev/null", False), ("pipe:[1234]", False), ("->0x176d28a71a1b69e9", False), ("", False),
    ("/home/me/log.txt", False),
])
def test_terminal_paths(path, expected):
    assert host.is_terminal_path(path) is expected


@pytest.mark.skipif(not (sys.platform == "darwin" or sys.platform.startswith("linux")), reason="POSIX only")
def test_stdout_of_a_real_process_in_a_terminal_and_in_a_pipe():
    import pty  # POSIX only; Windows has no pty or termios

    controller, terminal = pty.openpty()
    in_terminal = subprocess.Popen(["/bin/sleep", "10"], stdin=terminal, stdout=terminal, stderr=terminal)
    piped = subprocess.Popen(["/bin/sleep", "10"], stdout=subprocess.PIPE)
    try:
        time.sleep(0.3)
        assert host.stdout_is_terminal(in_terminal.pid) is True
        assert host.stdout_is_terminal(piped.pid) is False
    finally:
        for process in (in_terminal, piped):
            process.kill()
            process.wait()
        os.close(controller)
        os.close(terminal)


def test_the_answer_is_worked_out_once_and_a_failure_shows_the_card():
    calls = []

    def compute(environ):
        calls.append(environ)
        return host.TUI_REFUSAL

    cached = host.CachedQuestionRefusal({"A": "1"}, compute)
    assert cached() == host.TUI_REFUSAL and cached() == host.TUI_REFUSAL and len(calls) == 1

    def broken(environ):
        raise OSError("no process table")

    assert host.CachedQuestionRefusal({}, broken)() is None


def test_grants_refuse_with_the_surface_reason_before_any_card_or_lock():
    cards, taken = [], []
    grants = Grants(lambda params, cancel: cards.append(params), lambda: taken.append(1) or True, lambda: None,
                    question_refusal=lambda: host.TUI_REFUSAL)
    grants.can_ask = True
    with pytest.raises(Refusal) as refused:
        grants.ensure("chat-1", LOOK)
    assert str(refused.value) == host.TUI_REFUSAL and "BIOROUTER_CLI_CLASSIC=1" in str(refused.value)
    assert cards == [] and taken == [] and grants.owner is None


def test_the_refusal_names_both_ways_out():
    assert "desktop app" in host.TUI_REFUSAL and "BIOROUTER_CLI_CLASSIC=1" in host.TUI_REFUSAL
    assert consent.NO_CARD_REFUSAL != host.TUI_REFUSAL
