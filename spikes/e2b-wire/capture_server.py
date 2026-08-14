"""E2B SDK ワイヤキャプチャサーバー（spike M2a-1 実測用）。

127.0.0.1 の 3 ポートで待ち受け、全リクエストを JSONL に記録する:
  - 3000  : 制御プレーン (E2B_DEBUG=true 時の api_url 既定 http://localhost:3000)
  - 49983 : envd データプレーン (files / process)
  - 49999 : run_code (e2b-code-interpreter の JUPYTER_PORT)

レスポンスは「SDK を次のステップへ進める最小応答」を反復的に育てる。
未知のパスは 500 を返して SDK のエラーメッセージから期待形を推定する。
"""

import base64
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOG_PATH = "/private/tmp/claude-501/-Users-tt-git-oss-subaco-subaco/7dd16182-1485-46ab-a195-53f4824565cd/scratchpad/capture.jsonl"
LOG_LOCK = threading.Lock()

# --- 擬似サンドボックス状態（最小） ---------------------------------
SANDBOX_ID = "ic0dummysandboxid"  # create 応答で返す固定 ID
CLIENT_ID = "abcd1234"
ENVD_ACCESS_TOKEN = "envd-access-token-DUMMY"
TEMPLATE_ID = "code-interpreter-v1"
ENVD_VERSION = "0.5.5"
FILES = {}  # path -> bytes（files.write を read で返すため）


def log_event(ev):
    with LOG_LOCK:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def body_repr(b: bytes):
    if not b:
        return {"body": ""}
    try:
        return {"body": b.decode("utf-8")}
    except UnicodeDecodeError:
        return {"body_b64": base64.b64encode(b).decode()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_port_label = None  # サブクラスで設定

    def log_message(self, fmt, *args):  # 標準の stderr ログは抑止
        pass

    def _read_body(self) -> bytes:
        n = self.headers.get("Content-Length")
        if n:
            return self.rfile.read(int(n))
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _respond(self, status, headers, body: bytes, stream_lines=None):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if stream_lines is None:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        else:
            # 行区切りストリーミング（chunked）
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for line in stream_lines:
                data = (line + "\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write(b"0\r\n\r\n")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_ANY(self):
        body = self._read_body()
        port = self.server.server_address[1]
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        status, rheaders, rbody, stream = route(
            port, self.command, parsed.path, qs, dict(self.headers), body
        )
        ev = {
            "ts": time.strftime("%H:%M:%S"),
            "port": port,
            "method": self.command,
            "path": self.path,
            "http_version": self.request_version,
            "headers": dict(self.headers),
            **body_repr(body),
            "resp_status": status,
            "resp_headers": rheaders,
            "resp_body": (rbody[:2000].decode("utf-8", "replace") if rbody else "")
            + ("" if not stream else f"[STREAM {len(stream)} lines] " + " | ".join(stream)[:2000]),
        }
        log_event(ev)
        try:
            self._respond(status, rheaders, rbody, stream)
        except (ConnectionResetError, BrokenPipeError):
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_ANY


JSON_H = {"Content-Type": "application/json"}


def route(port, method, path, qs, headers, body):
    """(status, headers, body_bytes, stream_lines|None) を返す。"""

    # ============ 制御プレーン (3000) ============
    if port == 3000:
        if headers.get("X-API-KEY") != "e2b_0123456789abcdef0123456789abcdef":
            return (
                401,
                JSON_H,
                json.dumps({"code": 401, "message": "invalid api key"}).encode(),
                None,
            )
        if method == "POST" and path == "/sandboxes":
            resp = {
                "clientID": CLIENT_ID,
                "envdVersion": ENVD_VERSION,
                "sandboxID": SANDBOX_ID,
                "templateID": TEMPLATE_ID,
                "envdAccessToken": ENVD_ACCESS_TOKEN,
            }
            return 201, JSON_H, json.dumps(resp).encode(), None
        m = re.fullmatch(r"/sandboxes/([^/]+)", path)
        if m and method == "GET":
            resp = {
                "clientID": CLIENT_ID,
                "envdVersion": ENVD_VERSION,
                "sandboxID": SANDBOX_ID,
                "templateID": TEMPLATE_ID,
                "envdAccessToken": ENVD_ACCESS_TOKEN,
                "startedAt": "2026-08-14T00:00:00Z",
                "endAt": "2026-08-14T01:00:00Z",
                "state": "running",
                "cpuCount": 2,
                "memoryMB": 512,
                "diskSizeMB": 1024,
                "metadata": {"isolation_level": "shared-kernel"},
            }
            return 200, JSON_H, json.dumps(resp).encode(), None
        if m and method == "DELETE":
            return 204, {}, b"", None

    # ============ envd (49983) ============
    if port == 49983:
        if method == "POST" and path == "/init":
            return 204, {}, b"", None
        if method == "GET" and path == "/health":
            return 204, {}, b"", None
        if path == "/files":
            if headers.get("X-Access-Token") != ENVD_ACCESS_TOKEN:
                return (
                    401,
                    JSON_H,
                    json.dumps({"code": 401, "message": "unauthorized: missing or invalid access token"}).encode(),
                    None,
                )
            if method == "POST":
                # multipart はそのまま保存せず、素朴にパートを切り出す
                stored = parse_multipart_files(headers, body)
                entries = [
                    {"name": p.rsplit("/", 1)[-1], "type": "file", "path": p}
                    for p in stored
                ]
                return 200, JSON_H, json.dumps(entries).encode(), None
            if method == "GET":
                p = qs.get("path", [""])[0]
                if p not in FILES:
                    return (
                        404,
                        JSON_H,
                        json.dumps({"code": 404, "message": f"file not found: {p}"}).encode(),
                        None,
                    )
                data = FILES.get(p, b"")
                return 200, {"Content-Type": "application/octet-stream"}, data, None

    # ============ run_code (49999) ============
    if port == 49999:
        if method == "POST" and path == "/execute":
            req = json.loads(body or b"{}")
            if "RAISE_ERROR" in (req.get("code") or ""):
                lines = [
                    json.dumps({"type": "stderr", "text": "boom\n", "timestamp": 1}),
                    json.dumps(
                        {
                            "type": "error",
                            "name": "ValueError",
                            "value": "boom",
                            "traceback": "Traceback...\nValueError: boom\n",
                        }
                    ),
                ]
                return 200, {"Content-Type": "application/json"}, b"", lines
            lines = [
                json.dumps({"type": "stdout", "text": "2\n"}),
                json.dumps(
                    {
                        "type": "result",
                        "text": "2",
                        "is_main_result": True,
                    }
                ),
                json.dumps({"type": "end_of_execution"}),
            ]
            return 200, {"Content-Type": "application/json"}, b"", lines
        if method == "GET" and path == "/health":
            return 204, {}, b"", None

    # ---- 未知: 500 で返し SDK のエラーから期待形を推定する ----
    return (
        500,
        JSON_H,
        json.dumps({"message": f"capture-server: unknown {method} {path}"}).encode(),
        None,
    )


def parse_multipart_files(headers, body: bytes):
    """multipart/form-data から filename= のパートを FILES に保存し、パス一覧を返す。"""
    ctype = headers.get("Content-Type", "")
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    stored = []
    if not m:
        return stored
    boundary = ("--" + m.group(1)).encode()
    for part in body.split(boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        data = data.rsplit(b"\r\n", 1)[0]
        fm = re.search(rb'filename="([^"]*)"', head)
        if fm:
            name = fm.group(1).decode()
            FILES[name] = data
            stored.append(name)
    return stored


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    for p in (3000, 49983, 49999):
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    log_event({"ts": time.strftime("%H:%M:%S"), "event": "capture-server started", "ports": [3000, 49983, 49999]})
    print("capture server on 127.0.0.1:3000,49983,49999", flush=True)
    while True:
        time.sleep(3600)
