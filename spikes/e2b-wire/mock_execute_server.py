"""Mock run_code (port 49999) server: logs the exact request, streams JSON lines chunked."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_chunk(self, data: bytes):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        record = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": json.loads(body) if body else None,
        }
        print("REQUEST: " + json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)

        if self.path != "/execute":
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")
            return

        scenario = record["body"].get("code", "")

        if "SCENARIO_502" in scenario:
            msg = b"sandbox timeout"
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        if "SCENARIO_500" in scenario:
            msg = b"kaboom"
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        if "SCENARIO_STALL" in scenario:
            import time
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._send_chunk(b'{"type": "stdout", "text": "started\\n", "timestamp": 1}\n')
            time.sleep(5)  # longer than client execution timeout
            self._send_chunk(b'{"type": "number_of_executions", "execution_count": 1}\n')
            self.wfile.write(b"0\r\n\r\n")
            return
        if "SCENARIO_BLANK" in scenario:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._send_chunk(b'{"type": "stdout", "text": "a\\n", "timestamp": 1}\n')
            self._send_chunk(b"\n")  # blank line
            self._send_chunk(b'{"type": "number_of_executions", "execution_count": 1}\n')
            self.wfile.write(b"0\r\n\r\n")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        if "SCENARIO_ERROR" in scenario:
            events = [
                {"type": "stdout", "text": "before boom\n", "timestamp": 1755150000000000001},
                {"type": "error", "name": "NameError", "value": "name 'boom' is not defined",
                 "traceback": "Traceback (most recent call last):\n  ...\nNameError: name 'boom' is not defined\n"},
                {"type": "number_of_executions", "execution_count": 7},
            ]
        elif "SCENARIO_EDGE" in scenario:
            # unknown event type + trailing newline + blank line robustness probe
            events = [
                {"type": "stdout", "text": "edge\n", "timestamp": 2},
                {"type": "unknown_future_event", "text": "ignored?"},
                {"type": "end_of_execution"},
            ]
        else:
            events = [
                {"type": "stdout", "text": "hello stdout\n", "timestamp": 1755150000000000001},
                {"type": "stderr", "text": "hello stderr\n", "timestamp": 1755150000000000002},
                {"type": "result", "text": "42", "html": "<b>42</b>", "is_main_result": True},
                {"type": "result", "png": "aWZha2Vwbmc=", "is_main_result": False},
                {"type": "number_of_executions", "execution_count": 3},
            ]

        for ev in events:
            self._send_chunk((json.dumps(ev) + "\n").encode())
        # terminal chunk ends the stream (this is the only end-of-execution signal)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 49999
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("READY", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
