"""Fake JSON-RPC stdio server for tests. Replays one transcript file.

Usage: fake_app_server.py <transcript.json> <requests.log>
Every stdin line is appended to the log; id-1 initialize and id-2
account/rateLimits/read requests get the transcript's canned replies.
"""

import json
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        script = json.load(handle)
    log = open(sys.argv[2], "w", encoding="utf-8")
    mode = script.get("mode", "replay")
    if mode == "exit":
        return 1
    if mode == "hang":
        line = sys.stdin.readline()
        log.write(line)
        log.flush()
        time.sleep(60)
        return 0
    for raw in sys.stdin:
        log.write(raw)
        log.flush()
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        if req.get("method") == "initialized":
            continue
        rid = req.get("id")
        if req.get("method") == "initialize" and rid == 1:
            for noise in script.get("noise_before_init", []):
                sys.stdout.write(noise + "\n")
            sys.stdout.flush()
            if "init_error" in script:
                emit({"jsonrpc": "2.0", "id": 1, "error": script["init_error"]})
            else:
                emit({"jsonrpc": "2.0", "id": 1, "result": script.get("init_result", {})})
        elif req.get("method") == "account/rateLimits/read" and rid == 2:
            for noise in script.get("noise_before_read", []):
                sys.stdout.write(noise + "\n")
            sys.stdout.flush()
            if mode == "big":
                sys.stdout.write("x" * script.get("big_bytes", 2 * 1024 * 1024) + "\n")
                sys.stdout.flush()
            if "read_error" in script:
                emit({"jsonrpc": "2.0", "id": 2, "error": script["read_error"]})
            else:
                emit({"jsonrpc": "2.0", "id": 2, "result": script["read_result"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
