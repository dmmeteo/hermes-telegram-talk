"""A deterministic OpenAI-compatible model for the Pulse engine parity gate.

It stands in for the profile's provider only. Hermes' own plugin LLM facade (ctx.llm ->
agent.auxiliary_client.call_llm) calls it over HTTP exactly as it would call the profile's real
model; the Pulse prompts, parsing and validation all run unchanged. Nothing here is a real model,
and no paid or live provider is ever called. Its answers follow fixed rules on the material so
each engine mechanism can be shown to act on what the model said:

- selection ("You pick what Pulse brings up"): follow-ups value 5, titles with "invoice" or
  "lease" 4, failed work 2 (deliberately below the deterministic ranking), "routine" skipped as
  minor, titles the instructions list as not useful skipped as rated.
- memory ("note what is worth remembering"): "I'll"/"I will" from the user is a commitment (with
  "by YYYY-MM-DD" as its due date), from Hermes a promise; "tell me when" a watch; "thinking about"
  an interest. A line with FABRICATE also yields an item quoting words nobody said and a
  commitment quoted from Hermes's line, which the engine must reject.
- claims (part of the memory reply): a Hermes line saying "I set a reminder", "I sent the message",
  "I scheduled the job" or "I added the event" (optionally "I've"/"I have") is a claim of that action;
  the engine, not the fixture, judges it against the turn's stored tool calls.
- follow-up ("You check on something"): a later line on the item's subject (a shared word of five
  or more letters) saying "never mind" -> drop, "already did it" -> done; "rocket" ->
  wait; first check -> wait with a one-hour hint; later checks -> tell.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LABEL = "scripted-pulse-model"


def _parts(messages):
    user = next((m for m in messages if m.get("role") == "user"), {})
    content = user.get("content")
    if isinstance(content, str):
        return content, ""
    texts = [p.get("text", "") for p in content or [] if isinstance(p, dict)]
    return (texts[0] if texts else ""), "\n".join(texts[1:])


def _plain(title):
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def select(instructions, text):
    disliked = {_plain(t) for t in re.findall(r'"([^"]+)"', instructions.split("were not useful:", 1)[1].split(
        "\n", 1)[0])} if "were not useful:" in instructions else set()
    items = []
    for ref, rest in re.findall(r"^#(\d+) (.+)$", text, re.M):
        title = re.sub(r"^\[[^\]]+\] ", "", rest).split(" (", 1)[0].split(" — ", 1)[0]
        entry = {"ref": int(ref), "bring_up": True, "skip": "", "line": f"Spoken: {title}",
                 "why": f"fixture-model judged {title!r}"}
        if "routine" in title.lower():
            entry.update(bring_up=False, value=1, skip="minor")
        elif _plain(title) in disliked:
            entry.update(bring_up=False, value=1, skip="rated")
        elif rest.startswith("[follow-up"):
            entry["value"] = 5
        elif re.search(r"invoice|lease", title, re.I):
            entry["value"] = 4
        elif rest.startswith("[failed]"):
            entry["value"] = 2
        else:
            entry["value"] = 3
        items.append(entry)
    return {"items": items}


def _sentence(text, start):
    end = re.search(r"[.!?](?:\s|$)", text[start:])
    return text[start: start + end.end()].strip() if end else text[start:].strip()


def remember(text):
    chat = text.split("<<<chat", 1)[1] if "<<<chat" in text else text
    items = []
    for line, who, said in re.findall(r"^\[line (\d+)\] \S+ \S+ UTC (user|Hermes): (.*)$", chat, re.M):
        line = int(line)
        for match in re.finditer(r"\bI(?:'ll| will) ", said):
            evidence = _sentence(said, match.start())
            due = re.search(r"by (\d{4}-\d{2}-\d{2})", evidence)
            kind = "commitment" if who == "user" else "promise"
            items.append({"kind": kind, "text": f"{'The user' if who == 'user' else 'Hermes'} said: {evidence}",
                          "evidence": evidence, "line": line, "explicit": True,
                          "due": due.group(1) if due else None, "due_words": due.group(0) if due else None,
                          "same_as": None, "replaces": None})
        for phrase, kind in (("tell me when", "watch"), ("thinking about", "interest")):
            at = said.lower().find(phrase)
            if at >= 0 and who == "user":
                evidence = _sentence(said, at)
                items.append({"kind": kind, "text": f"The user: {evidence}", "evidence": evidence, "line": line,
                               "explicit": True, "due": None, "due_words": None})
        if "FABRICATE" in said:
            items.append({"kind": "fact", "text": "The user owns a yacht.", "evidence": "I own a yacht", "line": line})
            hermes = re.search(r"^\[line (\d+)\] \S+ \S+ UTC Hermes: (.*)$", chat, re.M)
            if hermes:
                items.append({"kind": "commitment", "text": "Misattributed.", "evidence": hermes.group(2)[:60],
                              "line": int(hermes.group(1))})
    claims = []
    for line, said in re.findall(r"^\[line (\d+)\] \S+ \S+ UTC Hermes: (.*)$", chat, re.M):
        for match in re.finditer(r"\bI(?:'ve| have)? (set a reminder|sent the message|scheduled the job|added the event)", said):
            evidence = _sentence(said, match.start())
            action = {"set a reminder": "reminder", "sent the message": "message", "scheduled the job": "job",
                      "added the event": "event"}[match.group(1)]
            claims.append({"action": action, "text": f"Hermes said: {evidence}", "evidence": evidence,
                           "line": int(line)})
    return {"items": items, "closed": [], "claims": claims}


def follow(text):
    checks = int((re.search(r"Checks so far: (\d+)", text) or [0, 0])[1])
    since = text.split("## Said in that chat since", 1)[-1].lower()
    item = (re.search(r"^\w+ #\d+: (.*)$", text, re.M) or [None, ""])[1]
    words = set(re.findall(r"[a-z]{5,}", item.lower())) - {"synthetic", "hermes", "about", "there", "would"}
    about = [line for line in since.splitlines() if words & set(re.findall(r"[a-z]{5,}", line))]
    if any("never mind" in line for line in about):
        return {"decision": "drop", "news": None, "why": "fixture: the user called it off", "check_again_hours": None}
    if any("already did it" in line for line in about):
        return {"decision": "done", "news": None, "why": "fixture: the user said it is done", "check_again_hours": None}
    if "rocket" in item.lower():
        return {"decision": "wait", "news": None, "why": "fixture: nothing shows it yet", "check_again_hours": None}
    if checks == 0:
        return {"decision": "wait", "news": None, "why": "fixture: too early", "check_again_hours": 1}
    return {"decision": "tell", "news": f"Follow-up: {item}", "why": "fixture: still open after a check",
            "check_again_hours": None}


def answer(messages):
    instructions, text = _parts(messages)
    if instructions.startswith("You pick what Pulse brings up"):
        return "selection", select(instructions, text)
    if "note what is worth remembering" in instructions:
        return "memory", remember(text)
    if instructions.startswith("You check on something"):
        return "followup", follow(text)
    return "other", {"note": "not a Pulse prompt"}


class PulseFixtureModel:
    def __init__(self):
        self.requests = []
        self.fail = False
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def kinds(self):
        with self.lock:
            return [k for k, _, _ in self.requests]

    def _handler(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": [{"id": LABEL, "object": "model"}]})
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "not found"}})
                messages = body.get("messages") or []
                kind, reply = answer(messages)
                with model.lock:
                    model.requests.append((kind, messages, body.get("response_format")))
                    failing = model.fail
                if failing:
                    return self._json(500, {"error": {"message": "synthetic outage", "type": "server_error"}})
                message = {"role": "assistant", "content": json.dumps(reply)}
                usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
                created = int(time.time())
                if not body.get("stream"):
                    return self._json(200, {"id": "chatcmpl-pulse", "object": "chat.completion", "created": created,
                                            "model": LABEL, "usage": usage,
                                            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in ({"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}):
                    chunk.update(id="chatcmpl-pulse", object="chat.completion.chunk", created=created, model=LABEL)
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler
