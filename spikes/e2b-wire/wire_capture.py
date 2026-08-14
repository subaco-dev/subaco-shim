"""envd データプレーン (49983) のワイヤ実測: SDK が実際に送るバイト列・ヘッダを mock サーバで捕捉。"""
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURED = []


def envelope(flags: int, data: bytes) -> bytes:
    return struct.pack(">BI", flags, len(data)) + data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _capture(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        CAPTURED.append(
            {
                "method": self.command,
                "path": self.path,
                "http_version": self.request_version,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        return body

    def _respond(self, payload: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._capture()
        if self.path.startswith("/files"):
            self._respond(b"hello from mock envd", "application/octet-stream")
        elif self.path.startswith("/health"):
            self._respond(b"", "text/plain", 204)
        else:
            self._respond(b"not found", "text/plain", 404)

    def do_POST(self):
        self._capture()
        if self.path == "/filesystem.Filesystem/Stat":
            payload = json.dumps(
                {"entry": {"name": "a", "type": "FILE_TYPE_FILE", "path": "/a"}}
            ).encode()
            self._respond(payload, "application/json")
        elif self.path == "/filesystem.Filesystem/ListDir":
            payload = json.dumps(
                {
                    "entries": [
                        {"name": "a", "type": "FILE_TYPE_FILE", "path": "/a"},
                        {"name": "d", "type": "FILE_TYPE_DIRECTORY", "path": "/d"},
                    ]
                }
            ).encode()
            self._respond(payload, "application/json")
        elif self.path == "/process.Process/Start":
            # connect+json server stream: start -> data(stdout) -> end -> EndStream
            body = b"".join(
                [
                    envelope(0, json.dumps({"event": {"start": {"pid": 42}}}).encode()),
                    envelope(
                        0,
                        json.dumps(
                            {
                                "event": {
                                    "data": {
                                        # bytes フィールドは JSON では base64
                                        "stdout": "aGkK"  # "hi\n"
                                    }
                                }
                            }
                        ).encode(),
                    ),
                    envelope(
                        0,
                        json.dumps(
                            {"event": {"end": {"exitCode": 0, "exited": True, "status": "exited"}}}
                        ).encode(),
                    ),
                    envelope(2, b"{}"),
                ]
            )
            self._respond(body, "application/connect+json")
        elif self.path.startswith("/files"):
            payload = json.dumps(
                [{"name": "t.txt", "type": "file", "path": "/home/user/t.txt"}]
            ).encode()
            self._respond(payload, "application/json")
        else:
            self._respond(b"{}", "application/json", 404)

    def log_message(self, *a):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    from packaging.version import Version

    from e2b.connection_config import ConnectionConfig
    from e2b.sandbox_sync.filesystem.filesystem import Filesystem
    from e2b.sandbox_sync.commands.command import Commands

    base = f"http://127.0.0.1:{port}"
    config = ConnectionConfig(
        api_key="e2b_dummy",
        sandbox_url=base,
        extra_sandbox_headers={
            "X-Access-Token": "envd-token-123",
            "E2b-Sandbox-Id": "sbx_test",
            "E2b-Sandbox-Port": "49983",
        },
    )
    envd_version = Version("1.2.3")

    fs = Filesystem(base, envd_version, config)
    cmds = Commands(base, config, envd_version)

    results = {}
    results["read"] = fs.read("/home/user/t.txt")
    results["write"] = str(fs.write("/home/user/t.txt", "data-abc"))
    results["list"] = [str(e) for e in fs.list("/home/user")]
    results["exists"] = fs.exists("/a")
    res = cmds.run("echo hi")
    results["run"] = {"exit": res.exit_code, "stdout": res.stdout, "stderr": res.stderr}

    server.shutdown()

    print("==== CLIENT RESULTS ====")
    print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    print("==== CAPTURED REQUESTS ====")
    for c in CAPTURED:
        print(f"--- {c['method']} {c['path']} ({c['http_version']})")
        for k, v in c["headers"].items():
            print(f"    {k}: {v}")
        body = c["body"]
        if body[:1] in (b"\x00", b"\x01", b"\x02", b"\x03"):
            flags, ln = struct.unpack(">BI", body[:5])
            print(f"    [envelope flags={flags} len={ln}] {body[5:5+ln]!r}")
            rest = body[5 + ln:]
            if rest:
                print(f"    [trailing bytes] {rest!r}")
        else:
            print(f"    BODY: {body[:600]!r}")


if __name__ == "__main__":
    main()
