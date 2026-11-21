"""A process table on macOS, Linux and Windows with the standard library only (no psutil).

Each row is (pid, parent pid, executable path or ""). Reading it needs no permission: the
executable paths of other users' processes may be missing, which only ever makes a rule
claim less, never more.
"""
import os
import subprocess
import sys
from collections import namedtuple

Process = namedtuple("Process", "pid ppid exe")


def _darwin():
    # `comm` is the executable path the process was started with; `-ww` keeps it whole.
    out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,ppid=,comm="], capture_output=True,
                         text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append(Process(pid, ppid, parts[2] if len(parts) > 2 else ""))
    return rows


def _linux():
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat = handle.read().decode("utf-8", "replace")
            # The command name is in parentheses and may itself contain spaces or ')'.
            fields = stat[stat.rindex(")") + 2:].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            exe = ""
        rows.append(Process(pid, ppid, exe))
    return rows


def _windows():  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    rows = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid, ppid = int(entry.th32ProcessID), int(entry.th32ParentProcessID)
            exe = ""
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    size = wintypes.DWORD(32768)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        exe = buffer.value
                finally:
                    kernel32.CloseHandle(handle)
            rows.append(Process(pid, ppid, exe))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def table():
    try:
        if sys.platform == "darwin":
            return _darwin()
        if sys.platform.startswith("linux"):
            return _linux()
        if sys.platform == "win32":
            return _windows()
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return []


def ancestry(pid, rows):
    """pid and its ancestors, nearest first, stopping before pid 0 and 1."""
    parents = {row.pid: row.ppid for row in rows}
    chain, cursor = [], pid
    while cursor and cursor > 1 and cursor not in chain:
        chain.append(cursor)
        cursor = parents.get(cursor)
    return chain


def descendants(root, rows):
    children = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found, stack = set(), [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found and child != root:
                found.add(child)
                stack.append(child)
    return found
