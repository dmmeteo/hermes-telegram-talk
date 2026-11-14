"""A scripted, deterministic OpenAI-compatible chat model for offline agent checks.

It stands in for the profile's inference provider only: Hermes' own AIAgent, tool registry, tool
execution and SessionDB run unchanged against it. Nothing here is a real model, and no paid or live
provider is ever called.

Talk's task-title call (receipts.TITLE_INSTRUCTIONS) gets {"title": ...} from TASK_TITLES, a fixed table.

Script, keyed on the newest user message:
  "read <path>"   -> a read_file tool call for <path>, then the tool's own output quoted back
  "recall"        -> the user/assistant turns the agent received, in order
  "slow <s>"      -> wait <s> seconds before answering (cancel/interrupt checks)
  "shell <cmd>"   -> a terminal tool call (approval checks)
  "slowshell <s> <cmd>" -> wait <s> seconds, then a terminal tool call (no late side effect after a stop)
  "big <n>"       -> a result of about <n> characters between BIG-START and BIG-END markers
  "t3start <name>" / "t3read <name>" -> a t3_control start of thread th-<name> / a read of it
  anything else   -> "SCRIPTED-ANSWER: <request>"
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LABEL = "scripted-deterministic-model"


def _last_user(messages):
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
            return (content or "").strip()
    return ""


def _request_line(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("TASK:"):
            return line[5:].strip()
    return text.strip().splitlines()[-1].strip() if text.strip() else ""


def is_title_request(messages):
    """Hermes' auxiliary session-title call, which also carries the opening user text."""
    first = messages[0].get("content") if messages else ""
    return isinstance(first, str) and first.startswith("You name chat sessions")


TASK_TITLES = {"перевір синтетичну збірку": "Check the synthetic build"}


def task_title_request(messages):
    """Talk's task-title call through the host ctx.llm (instructions, then the request): the request, else None."""
    content = messages[-1].get("content") if messages else None
    texts = [p.get("text", "") for p in content if isinstance(p, dict)] if isinstance(content, list) else []
    return texts[1].strip() if len(texts) == 2 and texts[0].startswith("You name a background task") else None


def plan(messages):
    """The scripted reply for a chat request: ("tool", name, args) or ("text", content)."""
    if is_title_request(messages):
        return ("text", "Synthetic voice task")
    if task_title_request(messages) is not None:
        return ("text", json.dumps({"title": TASK_TITLES.get(task_title_request(messages), "")}))
    if messages and messages[-1].get("role") == "tool":
        result = messages[-1].get("content") or ""
        if isinstance(result, list):
            result = " ".join(part.get("text", "") for part in result if isinstance(part, dict))
        return ("text", f"TOOL-RESULT-QUOTED: {result}")
    request = _request_line(_last_user(messages))
    word, _, rest = request.partition(" ")
    if word == "read" and rest:
        return ("tool", "read_file", {"path": rest.strip()})
    if word == "shell" and rest:
        return ("tool", "terminal", {"command": rest.strip()})
    if word == "slowshell" and rest:
        delay, _, command = rest.partition(" ")
        time.sleep(float(delay))
        return ("tool", "terminal", {"command": command.strip()})
    if word in ("t3start", "t3read") and rest:
        name = rest.strip()
        call = {"action": "start", "cwd": "/synthetic", "message": name, "request_id": f"req-start-{name}"} \
            if word == "t3start" else {"action": "read", "thread_id": f"th-{name}"}
        return ("tool", "t3_control", call)
    if word == "slow":
        time.sleep(float(rest or "5"))
        return ("text", "SCRIPTED-SLOW-DONE")
    if word == "big":
        size = int(rest or "5000")
        return ("text", "BIG-START " + ("synthetic research finding. " * (size // 28 + 1))[:size] + " BIG-END")
    if word == "recall":
        seen = [f"{m['role']}={m.get('content')}" for m in messages
                if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
        return ("text", "RECALL: " + " | ".join(seen))
    return ("text", f"SCRIPTED-ANSWER: {request}")


class ScriptedModel:
    def __init__(self):
        self.requests = []
        self.title_requests = []
        self.lock = threading.Lock()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": [{"id": LABEL, "object": "model"}]})
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "not found"}})
                messages = body.get("messages") or []
                with model.lock:
                    (model.title_requests if is_title_request(messages) else model.requests).append(messages)
                kind, *rest = plan(messages)
                if kind == "tool":
                    name, args = rest
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{len(model.requests)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": rest[0]}
                    finish = "stop"
                created = int(time.time())
                usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
                if not body.get("stream"):
                    return self._json(200, {"id": "chatcmpl-scripted", "object": "chat.completion", "created": created,
                                            "model": LABEL, "usage": usage,
                                            "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = {"role": "assistant"}
                if kind == "tool":
                    call = message["tool_calls"][0]
                    delta["tool_calls"] = [{"index": 0, **call}]
                else:
                    delta["content"] = message["content"]
                for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}):
                    chunk.update(id="chatcmpl-scripted", object="chat.completion.chunk", created=created, model=LABEL)
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler
