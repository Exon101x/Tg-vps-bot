"""Container HEALTHCHECK (see Dockerfile): poke the bot's own HTTP endpoint.

Uses HEALTH_PORT / PORT, the same variables the bot itself reads. When no port is
configured the endpoint is disabled, so there is nothing to check and we exit 0.
"""
import os
import socket
import sys

port = os.getenv("HEALTH_PORT") or os.getenv("PORT") or 0
try:
    port = int(port)
except ValueError:
    print(f"healthcheck: {port!r} is not a valid port number")
    sys.exit(1)
if not port:
    sys.exit(0)  # health endpoint disabled: nothing to poke

try:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
        data = s.recv(64)
except OSError as e:
    print(f"healthcheck: cannot reach 127.0.0.1:{port} ({e})")
    sys.exit(1)
sys.exit(0 if data else 1)
