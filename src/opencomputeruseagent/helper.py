"""Start, check, talk to and stop the Open Computer Use helper (`ocu mcp`, MCP over stdio)."""
import itertools
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import UPSTREAM_VERSION, __version__, tools

START_TIMEOUT = 30.0
CALL_TIMEOUT = 60.0
CANCEL_GRACE = 2.0
PROTOCOL_VERSION = "2025-03-26"

UNCERTAIN = ("The outcome is uncertain: run get_app_state to see what happened before trying again. "
             "Nothing is retried automatically.")


class HelperError(Exception):
    pass


class Cancelled(Exception):
    pass


class _WindowsJob:  # pragma: no cover - Windows only
    """A Job Object that ends the helper and everything it started (PowerShell) together."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObjectW failed")

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject failed")
        process_handle = kernel32.OpenProcess(0x0001 | 0x0100, False, process.pid)  # TERMINATE | SET_QUOTA
        if not process_handle or not kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError("AssignProcessToJobObject failed")
        kernel32.CloseHandle(process_handle)

    def terminate(self):
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class Helper:
    """One running helper process. Not shared between chats: the wrapper starts a new one when
    another chat takes the desktop, so no chat ever sees another's app state."""

    def __init__(self, payload, environ=None, log=None):
        self.payload = payload
        self.environ = os.environ if environ is None else environ
        self.log = log or (lambda message: None)
        self.private_dir = tempfile.mkdtemp(prefix="ocua-")
        self.process = None
        self._job = None
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self._slots = {}
        self._slots_lock = threading.Lock()
        self._alive = False
        self.server_info = None
        self.instructions = None
        self.stderr_tail = []

    # -- process lifetime -------------------------------------------------
    def start(self):
        from .payload import helper_environment

        manifest = self.payload.verify()
        env = helper_environment(self.payload.target, self.environ, self.private_dir, self.payload.namespace())
        args = [str(self.payload.executable)] + list(manifest.get("args") or ["mcp"])
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                      cwd=self.private_dir)
        if sys.platform == "win32":  # pragma: no cover - Windows only
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(args, **kwargs)
        if sys.platform == "win32":  # pragma: no cover - Windows only
            try:
                self._job = _WindowsJob(self.process)
            except OSError as error:
                self.log(f"job object unavailable: {error}")
        self._alive = True
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self._handshake()
        except Exception:
            self.stop()
            raise
        return self

    def _handshake(self):
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "opencomputeruseagent", "version": __version__},
        }, timeout=START_TIMEOUT)
        info = result.get("serverInfo") or {}
        if info.get("version") != UPSTREAM_VERSION:
            raise HelperError(f"The helper reports version {info.get('version')!r}, not {UPSTREAM_VERSION}. "
                              "Reinstall Open Computer Use Agent.")
        self.server_info = info
        self.instructions = result.get("instructions")
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {}, timeout=START_TIMEOUT)
        problems = tools.schema_mismatches(listed.get("tools") or [])
        if problems:
            raise HelperError("The helper's tools do not match the pinned contract (" + ", ".join(problems)
                              + "). Reinstall Open Computer Use Agent.")

    @property
    def alive(self):
        return self._alive and self.process is not None and self.process.poll() is None

    def stop(self):
        process, self._alive = self.process, False
        if process is not None and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            if self._job is not None:  # pragma: no cover - Windows only
                self._job.terminate()
            elif sys.platform != "win32":
                for sig, wait in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
                    try:
                        os.killpg(process.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        process.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            else:  # pragma: no cover - Windows only
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                               creationflags=0x08000000)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        self._fail_pending("The helper stopped. " + UNCERTAIN)
        try:
            import shutil

            shutil.rmtree(self.private_dir, ignore_errors=True)
        except OSError:
            pass

    # -- protocol ---------------------------------------------------------
    def _read_stdout(self):
        stream = self.process.stdout
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.log("helper wrote a line that is not JSON")
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._slots_lock:
                    slot = self._slots.get(message["id"])
                if slot is not None:
                    slot["message"] = message
                    slot["event"].set()
            elif "id" in message and "method" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not supported"}})
        self._alive = False
        self._fail_pending("The helper stopped unexpectedly. " + UNCERTAIN)

    def _read_stderr(self):
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]
                self.log("helper: " + line[:500])

    def _fail_pending(self, reason):
        with self._slots_lock:
            slots = list(self._slots.values())
        for slot in slots:
            if not slot["event"].is_set():
                slot["failure"] = reason
                slot["event"].set()

    def _send(self, message):
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError, AttributeError):
                raise HelperError("The helper is not running. " + UNCERTAIN) from None

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=CALL_TIMEOUT, cancel=None):
        """Send one request and wait. On a timeout the helper is ended (no replay); on a cancel
        the helper is told, given CANCEL_GRACE seconds, then ended; late results are dropped."""
        if not self.alive:
            raise HelperError("The helper is not running.")
        request_id = next(self._ids)
        slot = {"event": threading.Event(), "message": None, "failure": None}
        with self._slots_lock:
            self._slots[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.05):
                if cancel is not None and cancel.is_set():
                    self._cancel(request_id, slot)
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    self.log(f"{method} timed out after {timeout:.0f}s; ending the helper")
                    self.stop()
                    raise HelperError(f"The helper did not answer within {timeout:.0f} seconds and was "
                                      "stopped. " + UNCERTAIN)
            if slot["failure"]:
                raise HelperError(slot["failure"])
            message = slot["message"]
            if "error" in message:
                error = message["error"] or {}
                raise HelperError(str(error.get("message") or "The helper refused the request."))
            return message.get("result") or {}
        finally:
            with self._slots_lock:
                self._slots.pop(request_id, None)

    def _cancel(self, request_id, slot):
        """Tell the helper, then make sure it has settled within CANCEL_GRACE seconds.

        The helper answers nothing for a call it cancelled (measured), so a ping is the settle
        probe: it answers once the cancelled operation has stopped. No answer ends the helper.
        """
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": "cancelled by the host"})
        except HelperError:
            return
        if slot["event"].wait(0.05):
            return
        probe_id = next(self._ids)
        probe = {"event": threading.Event(), "message": None, "failure": None}
        with self._slots_lock:
            self._slots[probe_id] = probe
        try:
            self._send({"jsonrpc": "2.0", "id": probe_id, "method": "ping", "params": {}})
            settled = probe["event"].wait(CANCEL_GRACE) and probe["message"] is not None
        except HelperError:
            settled = False
        finally:
            with self._slots_lock:
                self._slots.pop(probe_id, None)
        if not settled:
            self.log("the helper did not settle a cancelled call; ending it")
            self.stop()

    def call_tool(self, name, arguments, timeout=CALL_TIMEOUT, cancel=None):
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout, cancel=cancel)


def run_cli(payload, environ, arguments, timeout=30.0):
    """Run one helper CLI command (`doctor --json`, `doctor`) with the scrubbed environment."""
    from .payload import helper_environment

    payload.verify()
    private_dir = tempfile.mkdtemp(prefix="ocua-cli-")
    env = helper_environment(payload.target, environ, private_dir, payload.namespace())
    kwargs = dict(capture_output=True, env=env, cwd=private_dir, timeout=timeout)
    if sys.platform == "win32":  # pragma: no cover - Windows only
        kwargs["creationflags"] = 0x08000000
    try:
        completed = subprocess.run([str(payload.executable)] + list(arguments), **kwargs)
        return completed.returncode, completed.stdout.decode("utf-8", "replace"), completed.stderr.decode("utf-8", "replace")
    finally:
        import shutil

        shutil.rmtree(private_dir, ignore_errors=True)
