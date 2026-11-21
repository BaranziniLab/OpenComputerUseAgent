#!/usr/bin/env python3
"""A scripted OpenAI compatible chat endpoint for tests/test_biorouter_install.py (no model).

A user message of the form  CALL <tool> <json-args>  makes the "model" call that tool.
After a tool result it answers with the first 400 characters of the result. Anything else
gets a short text answer. Usage: fake_openai.py <port> <log.jsonl>
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]


def log(**d):
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), **d}) + "\n")


def text_of(message):
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "gpt-4o", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        msgs = req.get("messages", [])
        tools = [t.get("function", {}).get("name") for t in req.get("tools", [])]
        last = msgs[-1] if msgs else {}
        reply_text, call = None, None
        if last.get("role") == "tool":
            reply_text = "Tool result: " + text_of(last)[:400]
        elif last.get("role") == "user" and "CALL " in text_of(last):
            spec = text_of(last)[text_of(last).rindex("CALL ") + 5:].strip().splitlines()[0]
            parts = spec.split(" ", 1)
            call = {"name": parts[0], "arguments": parts[1] if len(parts) > 1 else "{}"}
        elif not tools:
            reply_text = "Scripted chat"
        else:
            reply_text = "ok"
        log(path=self.path, stream=req.get("stream"), n_messages=len(msgs), n_tools=len(tools),
            tool_names=[t for t in tools if t and not t.startswith("developer")][:60], last_role=last.get("role"),
            call=call, reply=reply_text, req_bytes=int(self.headers.get("content-length", 0)))
        now = int(time.time())
        if req.get("stream"):
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            def chunk(delta, finish=None, usage=None):
                d = {"id": "chatcmpl-scripted", "object": "chat.completion.chunk", "created": now, "model": req.get("model"),
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage:
                    d["usage"] = usage
                self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n"); self.wfile.flush()
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if call:
                chunk({"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": f"call_{time.time_ns()}", "type": "function",
                       "function": {"name": call["name"], "arguments": call["arguments"]}}]})
                chunk({}, "tool_calls", usage)
            else:
                chunk({"role": "assistant", "content": reply_text})
                chunk({}, "stop", usage)
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        else:
            message = {"role": "assistant", "content": reply_text}
            if call:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{time.time_ns()}", "type": "function",
                           "function": {"name": call["name"], "arguments": call["arguments"]}}]}
            body = json.dumps({"id": "chatcmpl-scripted", "object": "chat.completion", "created": now, "model": req.get("model"),
                               "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if call else "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
