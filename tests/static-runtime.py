#!/usr/bin/env python3
"""Run an offline V8 calculation through the host's V1 stdio protocol.

Pass the host command as arguments, optionally preceded by a container launcher.
This is a runtime smoke check; it does not compile any Rust test binaries.
"""

import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time


def main(command):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, bufsize=0)
        deadline = time.monotonic() + 30

        def send(message):
            payload = json.dumps(message).encode()
            process.stdin.write(struct.pack("<I", len(payload)) + payload)

        def read_exact(size):
            result = bytearray()
            while len(result) < size:
                timeout = deadline - time.monotonic()
                if timeout <= 0 or not select.select([process.stdout], [], [], timeout)[0]:
                    raise TimeoutError("code-mode host response timed out")
                chunk = os.read(process.stdout.fileno(), size - len(result))
                if not chunk:
                    errors.seek(0)
                    raise RuntimeError(f"code-mode host closed stdout: {errors.read().decode()}")
                result.extend(chunk)
            return result

        def receive():
            size, = struct.unpack("<I", read_exact(4))
            if size > 64 * 1024 * 1024:
                raise ValueError("oversized response")
            return json.loads(read_exact(size))

        try:
            send({"type": "connection/hello", "supportedVersions": [1],
                  "requiredCapabilities": [], "optionalCapabilities": []})
            ready = receive()
            assert ready["type"] == "connection/ready", ready
            send({"type": "operation/request", "id": 1,
                  "request": {"method": "session/open", "sessionId": "static-smoke"}})
            opened = receive()
            assert opened["result"]["status"] == "ok", opened
            send({"type": "operation/request", "id": 2, "request": {
                "method": "session/execute", "sessionId": "static-smoke", "request": {
                    "tool_call_id": "static-smoke-1", "enabled_tools": [],
                    "source": 'text("static-nix-v8-" + (6 * 7))',
                    "yield_time_ms": 10000, "max_output_tokens": 256}}})
            frames = []
            for _ in range(10):
                frame = receive()
                frames.append(frame)
                if os.environ.get("CODEX_SMOKE_TRACE"):
                    print(json.dumps(frame), file=sys.stderr)
                if "static-nix-v8-42" in json.dumps(frame):
                    print("V8 execution passed: static-nix-v8-42")
                    return
                if frame.get("result", {}).get("status") == "error":
                    raise AssertionError(frames)
                runtime = frame.get("result", {}).get("value", {}).get("Result", {})
                if runtime.get("error_text") or frame["type"] == "cell/closed":
                    raise AssertionError(frames)
            raise AssertionError(frames)
        except Exception:
            errors.seek(0)
            print(errors.read().decode(), file=sys.stderr)
            raise
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: static-runtime.py HOST_COMMAND [ARGS...]")
    main(sys.argv[1:])
