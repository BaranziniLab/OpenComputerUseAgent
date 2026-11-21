"""The MCP server Biorouter talks to: newline delimited JSON-RPC over stdio.

It implements `initialize`, `ping`, `tools/list`, `tools/call` and `notifications/cancelled`,
and sends the client request `elicitation/create` for the consent card. Each tool call runs on
its own thread so the reader keeps taking messages, the card's answer among them.
"""
import itertools
import json
import os
import sys
import threading
import time

from . import SERVER_NAME, __version__, tools
from .consent import CARD_TIMEOUT

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-03-26"


class ClientRequestError(Exception):
    pass


class Server:
    def __init__(self, reader, writer, dispatcher_factory=None, log=None):
        self.reader = reader
        self.writer = writer
        self.log = log or (lambda message: None)
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._calls = {}
        self._calls_lock = threading.Lock()
        self.client_capabilities = {}
        if dispatcher_factory is None:
            from .dispatch import Dispatcher

            dispatcher_factory = lambda ask, log: Dispatcher(ask=ask, log=log)  # noqa: E731
        self.dispatcher = dispatcher_factory(self.ask, self.log)
        self._tools = tools.advertised()

    # -- output -----------------------------------------------------------
    def send(self, message):
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._write_lock:
            self.writer.write(line)
            self.writer.flush()

    def respond(self, request_id, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result if result is not None else {}
        self.send(message)

    # -- client requests (the consent card) -------------------------------
    def ask(self, params, cancel=None, timeout=CARD_TIMEOUT):
        """Send `elicitation/create` and wait for the person's answer."""
        if cancel is not None and cancel.is_set():
            return {"action": "cancel"}  # the call was cancelled before its card went out
        request_id = f"ocua-{next(self._ids)}"
        slot = {"event": threading.Event(), "message": None}
        with self._pending_lock:
            self._pending[request_id] = slot
        try:
            self.send({"jsonrpc": "2.0", "id": request_id, "method": "elicitation/create", "params": params})
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.1):
                if cancel is not None and cancel.is_set():
                    return {"action": "cancel"}
                if time.monotonic() >= deadline:
                    raise ClientRequestError("no answer")
            message = slot["message"]
            if "error" in message:
                raise ClientRequestError(str((message.get("error") or {}).get("message")))
            return message.get("result") or {}
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    # -- input ------------------------------------------------------------
    def serve(self):
        self.dispatcher.startup()
        try:
            for line in self.reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if isinstance(message, list):
                    for item in message:
                        self.handle(item)
                else:
                    self.handle(message)
        finally:
            with self._calls_lock:
                for cancel in self._calls.values():
                    cancel.set()
            self.dispatcher.shutdown()
        return 0

    def handle(self, message):
        if not isinstance(message, dict):
            return
        if "method" not in message:
            # A response to one of our requests.
            with self._pending_lock:
                slot = self._pending.get(message.get("id"))
            if slot is not None:
                slot["message"] = message
                slot["event"].set()
            return
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if method == "initialize":
            self.client_capabilities = params.get("capabilities") or {}
            self.dispatcher.grants.can_ask = "elicitation" in self.client_capabilities
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            self.respond(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Open Computer Use Agent", "version": __version__},
                "instructions": tools.INSTRUCTIONS,
            })
        elif method == "ping":
            if request_id is not None:
                self.respond(request_id, {})
        elif method == "tools/list":
            self.respond(request_id, {"tools": self._tools})
        elif method == "tools/call":
            cancel = threading.Event()
            with self._calls_lock:
                self._calls[request_id] = cancel
            # Admitted here, in the reader, so calls are admitted in the order they arrived.
            admit = getattr(self.dispatcher, "admit", None)
            name = params.get("name") if isinstance(params, dict) else None
            ticket = admit(name, _session_of(params)) if admit else None
            threading.Thread(target=self._run_call, args=(request_id, params, cancel, ticket), daemon=True).start()
        elif method == "notifications/cancelled":
            with self._calls_lock:
                cancel = self._calls.get(params.get("requestId"))
            if cancel is not None:
                cancel.set()
        elif method.startswith("notifications/"):
            return
        elif request_id is not None:
            self.respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def _run_call(self, request_id, params, cancel, ticket=None):
        try:
            session = _session_of(params)
            if ticket is not None:
                result = self.dispatcher.call(params.get("name"), params.get("arguments"), session, cancel,
                                              ticket=ticket)
            else:
                result = self.dispatcher.call(params.get("name"), params.get("arguments"), session, cancel)
        except Exception as error:  # a bug must still answer the call
            self.log(f"tool call failed: {error!r}")
            result = {"content": [{"type": "text", "text": f"Open Computer Use Agent failed: {error}"}],
                      "isError": True}
        finally:
            with self._calls_lock:
                self._calls.pop(request_id, None)
        if cancel.is_set():
            # A cancelled request is not answered, its late result is dropped, and the cancel
            # ends the chat's access even when it arrived after the call had finished.
            on_cancel = getattr(self.dispatcher, "cancelled", None)
            if on_cancel is not None:
                on_cancel(_session_of(params))
            return
        self.respond(request_id, result)


def _session_of(params):
    meta = params.get("_meta") if isinstance(params, dict) else None
    session = meta.get("biorouter-session-id") if isinstance(meta, dict) else None
    return session if isinstance(session, str) and session else None


def _log(message):
    try:
        sys.stderr.write(f"[opencomputeruseagent] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def run_stdio():
    reader = open(sys.stdin.fileno(), "r", encoding="utf-8", errors="replace", closefd=False, newline="\n")
    writer = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False, newline="\n")
    # Nothing else may write to stdout: it carries the protocol.
    sys.stdout = sys.stderr
    server = Server(reader, writer, log=_log)

    def stop(_signum, _frame):
        # Biorouter ends an extension with a signal; end the helper with it, not after it.
        try:
            server.dispatcher.shutdown()
        finally:
            os._exit(0)

    import signal

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (OSError, ValueError):
                pass
    try:
        return server.serve()
    except KeyboardInterrupt:
        server.dispatcher.shutdown()
        return 0
