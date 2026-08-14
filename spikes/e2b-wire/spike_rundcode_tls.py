"""決定実験: run_code 非デバッグ経路のローカル終端。

手法: 偽制御プレーン (http://127.0.0.1:7799) が create 応答の domain に
"localhost:8443" (ポート埋め込み) を返す。*.localhost は macOS で 127.0.0.1 に
解決されるため、SDK が組む run_code URL https://49999-{id}.localhost:8443 が
ローカル TLS サーバーに到達する。TLS 信頼は SSL_CERT_FILE (httpx trust_env 既定)。
"""
import http.server
import json
import os
import ssl
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "tls_cert.pem")
KEY = os.path.join(HERE, "tls_key.pem")


def _ensure_certs():
    """*.sbx.localhost のワイルドカード自己署名証明書を無ければ生成する（鍵はコミットしない）。"""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    import subprocess

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", KEY, "-out", CERT, "-days", "30", "-nodes",
            "-subj", "/CN=*.sbx.localhost",
            "-addext", "subjectAltName=DNS:*.sbx.localhost",
        ],
        check=True,
        capture_output=True,
    )


_ensure_certs()

os.environ["E2B_API_URL"] = "http://127.0.0.1:7799"
os.environ["E2B_API_KEY"] = "e2b_" + "ab" * 16
os.environ["SSL_CERT_FILE"] = CERT

SANDBOX_ID = "sbx-tls-0001"
DOMAIN = "sbx.localhost:8443"  # ポート埋め込み domain（*.sbx.localhost = 3 ラベルで OpenSSL のワイルドカード制約を満たす）

log = []


class ControlPlane(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        log.append(("CTRL", "POST", self.path, dict(self.headers), body.decode()))
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
        log.append(("CTRL", "DELETE", self.path, dict(self.headers), ""))
        self.send_response(204)
        self.end_headers()


class RunCode(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        log.append(("DATA", "POST", self.path, dict(self.headers), body.decode()))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in [
            {"type": "stdout", "text": "2\n", "timestamp": 1723600000000000000},
            {"type": "result", "text": "2", "is_main_result": True},
            {"type": "number_of_executions", "execution_count": 1},
        ]:
            line = (json.dumps(ev) + "\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
        self.wfile.write(b"0\r\n\r\n")


ctrl = http.server.ThreadingHTTPServer(("127.0.0.1", 7799), ControlPlane)
threading.Thread(target=ctrl.serve_forever, daemon=True).start()

data = http.server.ThreadingHTTPServer(("127.0.0.1", 8443), RunCode)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
data.socket = ctx.wrap_socket(data.socket, server_side=True)
threading.Thread(target=data.serve_forever, daemon=True).start()

from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(template="code-interpreter-v1")
print("sandbox_id:", sbx.sandbox_id, "domain:", sbx.sandbox_domain)
print("_jupyter_url:", sbx._jupyter_url)
ex = sbx.run_code("1+1")
print("execution.text:", ex.text, "| stdout:", ex.logs.stdout, "| count:", ex.execution_count)
sbx.kill()

print("\n--- captured requests ---")
for plane, meth, path, hdrs, body in log:
    print(plane, meth, path)
    interesting = {k: v for k, v in hdrs.items() if k.lower() in
                   ("host", "x-api-key", "x-access-token", "e2b-sandbox-id",
                    "e2b-traffic-access-token", "content-type")}
    print("   ", interesting)
    if body:
        print("   body:", body[:200])

ok = ex.text == "2" and ex.logs.stdout == ["2\n"]
print("\nRESULT:", "GREEN" if ok else "FAIL")
sys.exit(0 if ok else 1)
