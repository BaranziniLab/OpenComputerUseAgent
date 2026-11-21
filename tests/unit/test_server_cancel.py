"""The server's handling of a cancel, with a stand-in dispatcher (no helper, no threads racing)."""
import io
import json
import threading

from opencomputeruseagent.server import Server


class LateCancelDispatcher:
    """A call that finishes, but whose cancel lands before the server could answer it."""

    def __init__(self, ask, log):
        self.ended, self.admitted = [], []
        self.grants = type("G", (), {"can_ask": False})()
        self.done = threading.Event()

    def startup(self):
        pass

    def shutdown(self):
        pass

    def admit(self, name, session):
        self.admitted.append((name, session))
        return "ticket"

    def call(self, name, arguments, session, cancel, ticket=None):
        assert ticket == "ticket"
        cancel.set()  # the cancel arrives just after the helper answered
        return {"content": [{"type": "text", "text": "late"}]}

    def cancelled(self, session):
        self.ended.append(session)
        self.done.set()


def test_a_late_cancel_drops_the_result_and_ends_the_chats_access():
    out = io.StringIO()
    server = Server(io.StringIO(), out, dispatcher_factory=LateCancelDispatcher)
    server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "click", "arguments": {}, "_meta": {"biorouter-session-id": "chat-9"}}})
    assert server.dispatcher.done.wait(5)
    assert server.dispatcher.admitted == [("click", "chat-9")]
    assert server.dispatcher.ended == ["chat-9"]
    assert "late" not in out.getvalue()


def test_calls_are_admitted_in_the_order_they_arrive():
    out = io.StringIO()
    server = Server(io.StringIO(), out, dispatcher_factory=LateCancelDispatcher)
    for request_id, name in ((1, "click"), (2, "type_text"), (3, "press_key")):
        server.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                       "params": {"name": name, "_meta": {"biorouter-session-id": "c"}}})
    assert [name for name, _ in server.dispatcher.admitted] == ["click", "type_text", "press_key"]
    json.dumps(server.dispatcher.admitted)


def test_a_call_whose_params_are_not_an_object_is_answered_and_the_reader_survives():
    out = io.StringIO()
    server = Server(io.StringIO(), out, dispatcher_factory=LateCancelDispatcher)
    server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": [1, 2]})
    deadline = threading.Event()
    for _ in range(50):
        if out.getvalue():
            break
        deadline.wait(0.05)
    answer = json.loads(out.getvalue().splitlines()[0])
    assert answer["id"] == 5 and answer["result"]["isError"] is True
    server.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert '"id":6' in out.getvalue()
