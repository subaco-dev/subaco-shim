# URL 構成マトリクスと E2B_DEBUG 挙動の実測
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from e2b.connection_config import ConnectionConfig

SID = "sbx-1"

print("=== ConnectionConfig URL matrix ===")

# 1) 既定（env なし相当）
for k in ("E2B_DOMAIN", "E2B_DEBUG", "E2B_API_URL", "E2B_SANDBOX_URL"):
    os.environ.pop(k, None)
c = ConnectionConfig(api_key="e2b_" + "ab" * 20)
print("[default] domain:", c.domain, "api_url:", c.api_url)
print("[default] sandbox_url(custom dom):", c.get_sandbox_url(SID, "cube.local"))
print("[default] sandbox_url(e2b.app):", c.get_sandbox_url(SID, "e2b.app"))
print("[default] direct_url(e2b.app):", c.get_sandbox_direct_url(SID, "e2b.app"))
print("[default] get_host(49999, cube.local):", c.get_host(SID, "cube.local", 49999))

# 2) E2B_DOMAIN のみ
c = ConnectionConfig(api_key="e2b_" + "ab" * 20, domain="cube.local")
print("[E2B_DOMAIN=cube.local] api_url:", c.api_url)

# 3) debug=true
c = ConnectionConfig(api_key="e2b_" + "ab" * 20, debug=True)
print("[debug] api_url:", c.api_url)
print("[debug] sandbox_url:", c.get_sandbox_url(SID, "cube.local"))
print("[debug] direct_url:", c.get_sandbox_direct_url(SID, "cube.local"))
print("[debug] get_host(49999):", c.get_host(SID, "cube.local", 49999))

# 4) debug=true + E2B_API_URL 上書き
c = ConnectionConfig(api_key="e2b_" + "ab" * 20, debug=True, api_url="http://127.0.0.1:7777")
print("[debug+api_url] api_url:", c.api_url)

# 5) E2B_SANDBOX_URL 上書き
c = ConnectionConfig(api_key="e2b_" + "ab" * 20, sandbox_url="http://127.0.0.1:8888")
print("[sandbox_url] sandbox_url:", c.get_sandbox_url(SID, "cube.local"))
print("[sandbox_url] direct_url:", c.get_sandbox_direct_url(SID, "cube.local"))
print("[sandbox_url] get_host(49999):", c.get_host(SID, "cube.local", 49999), "(<- run_code は影響なし)")

print()
print("=== E2B_DEBUG=true での create / kill / get_info ===")
CAPTURED = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cap(self):
        CAPTURED.append((self.command, self.path))

    def do_GET(self):
        self._cap()
        payload = json.dumps(
            {
                "sandboxID": "debug_sandbox_id",
                "clientID": "c",
                "templateID": "t",
                "envdVersion": "0.6.4",
                "cpuCount": 1,
                "memoryMB": 256,
                "diskSizeMB": 512,
                "startedAt": "2026-08-14T00:00:00Z",
                "endAt": "2026-08-14T01:00:00Z",
                "state": "running",
                "metadata": {},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ["E2B_API_KEY"] = "e2b_" + "ab" * 20
os.environ["E2B_DEBUG"] = "true"
os.environ["E2B_API_URL"] = f"http://127.0.0.1:{port}"

from e2b import Sandbox

sbx = Sandbox.create(template="whatever")
print("debug create -> sandbox_id:", sbx.sandbox_id)
print("debug create -> envd_api_url:", sbx.envd_api_url)
print("debug create -> sandbox_domain:", sbx.sandbox_domain)
print("debug create -> sandbox_headers:", sbx.connection_config.sandbox_headers)
print("debug kill:", sbx.kill())
print("requests so far (should be 0):", CAPTURED)
info = sbx.get_info()  # debug でも get_info は API を叩くか？
print("debug get_info state:", info.state, "metadata:", info.metadata)
print("requests after get_info:", CAPTURED)

# code-interpreter の run_code URL (debug)
from e2b_code_interpreter import Sandbox as CISandbox

ci = CISandbox.create(template="whatever")
print("debug jupyter url:", ci._jupyter_url)
server.shutdown()
