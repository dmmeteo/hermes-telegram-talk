"""Run one native Hermes command and keep a bounded record of its stream-json stdout.

    python spool.py OUTPUT -- COMMAND...

The command inherits this process's stdin (the task prompt) and its process group, so it outlives
the Talk server like before. Only its stdout passes through here: the session event and the final
result event are kept (the result text up to TEXT_MAX UTF-8 bytes, marked when cut), every other
line is counted and dropped. OUTPUT is rewritten atomically and never grows past a few MiB, however
much the run prints. Files the run itself writes are not limited. The exit code is the command's.
"""
import json
import os
import signal
import subprocess
import sys

LINE_MAX = 8 * 1024 * 1024
TEXT_MAX = 1024 * 1024
ERROR_MAX = 4000


def cut(text, limit):
    """text within limit UTF-8 bytes, never splitting a character; (kept, characters dropped)."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, 0
    kept = raw[:limit].decode("utf-8", "ignore")
    return kept, len(text) - len(kept)


def write(path, record):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace") as handle:
        for key in ("system", "result", "spool"):
            if record.get(key) is not None:
                handle.write(json.dumps(record[key], ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def keep(record, raw):
    if not raw.startswith(b"{"):
        return False
    try:
        event = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    if event.get("type") == "system" and event.get("session_id"):
        record["system"] = {"type": "system", "session_id": str(event["session_id"])}
        return True
    if event.get("type") == "result":
        text, dropped = cut(str(event.get("text") or ""), TEXT_MAX)
        kept = {"type": "result", "exit_code": event.get("exit_code"), "text": text,
                "error": cut(str(event.get("error") or ""), ERROR_MAX)[0], "session_id": str(event.get("session_id") or "")}
        if dropped:
            kept["text_cut"] = dropped
        record["result"] = kept
        return True
    return False


def main(argv):
    if len(argv) < 4 or argv[2] != "--":
        print("usage: spool.py OUTPUT -- COMMAND...", file=sys.stderr)
        return 2
    path, command = argv[1], argv[3:]
    record = {"spool": {"type": "spool", "bytes": 0, "lines": 0, "oversized": 0, "exit": None}}
    write(path, record)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # Cancel signals the whole group; the command handles them and this process keeps reading
    # until it has written its final result.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    stats, pending, skipping = record["spool"], b"", False
    while True:
        chunk = child.stdout.read1(65536)
        if not chunk:
            break
        stats["bytes"] += len(chunk)
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            stats["lines"] += 1
            if skipping:
                skipping = False
            elif keep(record, line):
                write(path, record)
        if len(pending) > LINE_MAX:
            stats["oversized"] += 1
            pending, skipping = b"", True
    if pending and not skipping:
        stats["lines"] += 1
        keep(record, pending)
    code = child.wait()
    stats["exit"] = code
    write(path, record)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
