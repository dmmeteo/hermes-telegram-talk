"""A real subprocess standing in for `hermes chat` in the spool check: it floods stdout with stream-json,
writes a large legitimate artifact in its working directory, then prints its final result."""
import json
import os
import sys

MODE = os.environ.get("NOISY_MODE", "flood")
sys.stdin.read()
print(json.dumps({"type": "system", "session_id": "synthetic-noisy-session"}), flush=True)
chunk = json.dumps({"type": "delta", "text": "n" * 65000})
for _ in range(int(os.environ.get("NOISY_MIB", "24")) * 16):
    print(chunk)
if MODE == "oversized":
    sys.stdout.write("{\"type\": \"delta\", \"text\": \"" + "o" * (9 * 1024 * 1024) + "\"}\n")
with open("noisy-artifact.bin", "wb") as artifact:
    artifact.write(os.urandom(6 * 1024 * 1024))
text = "NOISY-RESULT-START " + ("ж" * 700000 if MODE == "wide" else "r" * 200000) + " NOISY-RESULT-END"
print(json.dumps({"type": "result", "exit_code": 0, "text": text, "session_id": "synthetic-noisy-session"}), flush=True)
