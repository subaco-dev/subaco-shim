# E2B 制御プレーン spike: 偽 API サーバーで SDK の実リクエストを採取する
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURED = []

SANDBOX_ID = "sbx-cube-0001"
ENVD_TOKEN = "envd-token-abc123"
TRAFFIC_TOKEN = "traffic-token-xyz789"
DOMAIN = "cube.local"  # 非サポートドメイン（sandbox.{domain} 集約経路に入らないことを確認）


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _capture(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        CAPTURED.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        return body

    def _reply(self, code, obj=None):
        payload = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self):
        self._capture()
        if self.path == "/sandboxes":
            self._reply(
                201,
                {
                    "sandboxID": SANDBOX_ID,
                    "clientID": "client-0001",
                    "templateID": "test-tmpl",
                    "envdVersion": "0.6.4",
                    "envdAccessToken": ENVD_TOKEN,
                    "trafficAccessToken": TRAFFIC_TOKEN,
                    "domain": DOMAIN,
                },
            )
        elif self.path.endswith("/connect"):
            self._reply(
                200,
                {
                    "sandboxID": SANDBOX_ID,
                    "clientID": "client-0001",
                    "templateID": "test-tmpl",
                    "envdVersion": "0.6.4",
                    "envdAccessToken": ENVD_TOKEN,
                    "trafficAccessToken": TRAFFIC_TOKEN,
                    "domain": DOMAIN,
                },
            )
        else:
            self._reply(404, {"code": 404, "message": "not found"})

    def do_GET(self):
        self._capture()
        if self.path == f"/sandboxes/{SANDBOX_ID}":
            self._reply(
                200,
                {
                    "sandboxID": SANDBOX_ID,
                    "clientID": "client-0001",
                    "templateID": "test-tmpl",
                    "envdVersion": "0.6.4",
                    "cpuCount": 2,
                    "memoryMB": 512,
                    "diskSizeMB": 1024,
                    "startedAt": "2026-08-14T00:00:00Z",
                    "endAt": "2026-08-14T01:00:00Z",
                    "state": "running",
                    "metadata": {"isolation_level": "shared-kernel"},
                    "domain": DOMAIN,
                    "envdAccessToken": ENVD_TOKEN,
                },
            )
        else:
            self._reply(404, {"code": 404, "message": "not found"})

    def do_DELETE(self):
        self._capture()
        self._reply(204)

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

import os

os.environ["E2B_API_KEY"] = "e2b_" + "ab" * 20
os.environ["E2B_API_URL"] = f"http://127.0.0.1:{port}"
os.environ.pop("E2B_DEBUG", None)
os.environ.pop("E2B_DOMAIN", None)
os.environ.pop("E2B_SANDBOX_URL", None)

from e2b import Sandbox

print("=== create ===")
sbx = Sandbox.create(template="test-tmpl", metadata={"k": "v"}, envs={"E": "1"}, timeout=123)
print("sandbox_id:", sbx.sandbox_id)
print("sandbox_domain:", sbx.sandbox_domain)
print("envd_api_url:", sbx.envd_api_url)
print("envd_direct_url:", sbx.envd_direct_url)
print("get_host(49999):", sbx.get_host(49999))
print("traffic_access_token:", sbx.traffic_access_token)
print("sandbox_headers:", sbx.connection_config.sandbox_headers)

print("=== get_info ===")
info = sbx.get_info()
print("info:", info)

print("=== connect (classmethod) ===")
sbx2 = Sandbox.connect(SANDBOX_ID)
print("connected envd_api_url:", sbx2.envd_api_url)

print("=== kill ===")
print("killed:", sbx.kill())

print()
print("=== CAPTURED REQUESTS ===")
for i, r in enumerate(CAPTURED):
    print(f"--- [{i}] {r['method']} {r['path']}")
    interesting = {
        k: v
        for k, v in r["headers"].items()
        if k.lower()
        in (
            "x-api-key",
            "authorization",
            "user-agent",
            "content-type",
            "lang",
            "lang_version",
            "package_version",
            "publisher",
            "sdk_runtime",
            "system",
            "connection",
            "host",
        )
    }
    print("    headers:", json.dumps(interesting, ensure_ascii=False))
    if r["body"]:
        print("    body:", r["body"])

server.shutdown()
