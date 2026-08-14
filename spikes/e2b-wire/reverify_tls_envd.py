"""REVERIFY claim 9: 非デバッグ domain 経路で envd 要求の Host が {port}-{sandbox_id}.{domain} になり、
同一リスナー (127.0.0.1:8443) 上で run_code (49999-*) と envd (49983-*) が Host で識別できるか。"""
import http.server
import json
import os
import ssl
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "tls_cert.pem")
KEY = os.path.join(HERE, "tls_key.pem")

os.environ["E2B_API_URL"] = "http://127.0.0.1:7798"
os.environ["E2B_API_KEY"] = "e2b_" + "ab" * 16
os.environ["SSL_CERT_FILE"] = CERT
for k in ("E2B_DEBUG", "E2B_DOMAIN", "E2B_SANDBOX_URL"):
    os.environ.pop(k, None)

SANDBOX_ID = "sbx-tls-0001"
DOMAIN = "sbx.localhost:8443"
log = []


class ControlPlane(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        resp = json.dumps({
            "sandboxID": SANDBOX_ID,
            "clientID": "dummy",
            "templateID": "code-interpreter-v1",
            "envdVersion": "0.5.5",
            "envdAccessToken": "tok-tls-xyz",
            "domain": DOMAIN,
        }).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()


class DataPlane(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _grab(self, body=b""):
        log.append((self.command, self.path, dict(self.headers)))

    def do_GET(self):
        self._grab()
        if self.path.startswith("/files"):
            data = b"tls-envd-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self._grab()
        if self.path == "/execute":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for ev in [{"type": "result", "text": "2", "is_main_result": True}]:
                line = (json.dumps(ev) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.write(b"0\r\n\r\n")
        elif self.path.startswith("/files"):
            payload = json.dumps([{"name": "f.txt", "type": "file", "path": "/tmp/f.txt"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


ctrl = http.server.ThreadingHTTPServer(("127.0.0.1", 7798), ControlPlane)
threading.Thread(target=ctrl.serve_forever, daemon=True).start()

data = http.server.ThreadingHTTPServer(("127.0.0.1", 8443), DataPlane)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
data.socket = ctx.wrap_socket(data.socket, server_side=True)
threading.Thread(target=data.serve_forever, daemon=True).start()

from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(template="code-interpreter-v1")
print("envd_api_url:", sbx.envd_api_url)
w = sbx.files.write("/tmp/f.txt", "x")
r = sbx.files.read("/tmp/f.txt")
ex = sbx.run_code("1+1")
sbx.kill()

print("files.write:", w, "| files.read:", r, "| run_code:", ex.text)
print("--- data plane requests (single listener 127.0.0.1:8443) ---")
for meth, path, hdrs in log:
    keys = {k: v for k, v in hdrs.items() if k.lower() in
            ("host", "x-access-token", "e2b-sandbox-id", "e2b-sandbox-port")}
    print(meth, path, keys)

hosts = {h.get("Host") for _, _, h in log}
ok = (r == "tls-envd-bytes" and ex.text == "2"
      and any(h.startswith("49983-" + SANDBOX_ID) for h in hosts)
      and any(h.startswith("49999-" + SANDBOX_ID) for h in hosts))
print("RESULT:", "GREEN" if ok else "FAIL")
sys.exit(0 if ok else 1)
