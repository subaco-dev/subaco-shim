"""ローカル HTTP シムサーバー（E2B ワイヤ互換・127.0.0.1 限定 bind）。

2 プレーン構成（spike——docs/00-memo/05_spike結果_E2B_ワイヤ.md——で実測確定）:

- **制御プレーン**（平文 HTTP・``E2B_API_URL``・``X-API-KEY``）:
  ``POST /sandboxes``(201)・``GET /sandboxes/{id}``(200)・``DELETE /sandboxes/{id}``(204/404)。
  エラーボディは ``{"code": int, "message": str}``。
- **データプレーン**（単一 TLS リスナー・``*.sbx.localhost``・``X-Access-Token``）:
  Host ヘッダ ``{port}-{sandbox_id}.sbx.localhost`` でルーティングし、
  49983 面は ``GET/POST /files``・``GET /health``、49999 面は ``POST /execute``
  （chunked HTTP/1.1・改行区切り JSON ストリーム）を受ける。

責務:

- **127.0.0.1 限定 bind**。ループバック以外へは bind しない。
- **トークン検証**: 制御プレーン = ``X-API-KEY``、データプレーン = ``X-Access-Token``
  （サンドボックス単位・Host の sandbox_id との対応を検証）。なし／不一致は両経路で **401**。
- **default-deny enforce**: create 時に実行先ドライバの隔離レベルで
  :func:`~subaco_shim.isolation.route_execution` を評価。shared-kernel は
  ``allow_shared_kernel`` オプトイン時のみ許可、unknown は無条件拒否、vm-per-container 以上は
  無条件許可。**エージェント申告の trust では緩和しない**（route_execution は trust を取らない）。
  拒否は **403**。
- **ドライバへのルーティング**と、get_info の metadata に ``isolation_level`` を載せて返却、
  デバッグ用 ``X-Isolation-Level`` ヘッダも付す。

:class:`ShimApp` のディスパッチは純関数的（ソケット非依存）でユニットテスト可能。HTTP バインドは
:func:`make_server`（ループバック強制。データプレーンは ssl_context 付き）で行う。
stdlib のみに依存する。
"""

from __future__ import annotations

import contextlib
import email.parser
import email.policy
import hmac
import json
import secrets
import select
import socket
import ssl
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from .drivers.base import Driver, ExecutionHandle
from .isolation import route_execution
from .logging import get_logger
from .models import Execution
from .protocol import wire
from .protocol.routes import Route, UnknownRoute, resolve_control, resolve_data

_log = get_logger("server")

# ループバック以外への bind を拒否するための許可ホスト（127.0.0.1 限定 bind）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# SandboxDetail の公称リソース値。ローカルシムはホスト資源を直接使うため意味を持たないが、
# SDK 必須 10 キーの一部（欠落は KeyError で SDK クラッシュ——spike §1.1）。
_NOMINAL_CPU_COUNT = 1
_NOMINAL_MEMORY_MB = 512
_NOMINAL_DISK_MB = 1024

# create 要求に timeout が無い場合の既定生存秒（endAt の算出にのみ使う）。
_DEFAULT_SANDBOX_TIMEOUT = 300.0


def _rfc3339(ts: float) -> str:
    """UNIX 時刻を RFC3339（Z サフィックス）へ。SandboxDetail の startedAt / endAt 用。"""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Request:
    """ディスパッチ入力（HTTP から切り離した最小表現）。"""

    method: str
    path: str  # クエリを含まないパス部分
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        """ヘッダを大小文字非依存で取得する。"""
        lname = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lname:
                return v
        return None

    @classmethod
    def from_raw(cls, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Request:
        """生のパス（クエリ付き）とヘッダから :class:`Request` を組む。"""
        split = urlsplit(raw_path)
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        return cls(
            method=method,
            path=split.path,
            headers=dict(headers),
            query=query,
            body=body,
        )


@dataclass
class Response:
    """ディスパッチ出力。``stream`` があれば chunked 転送（/execute の JSON lines）。

    ``execution`` は run_code の未完了ハンドル: HTTP 層（:class:`_Handler`）が完了を
    待機しつつクライアント切断を監視し、切断検出時に ``cancel()`` を呼ぶ
    （クライアント TCP 切断 = 実行キャンセル——spike §1.3）。ディスパッチ層は
    ソケット非依存を保つため、待機・監視は HTTP 層の責務とする。
    """

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"
    stream: Iterator[bytes] | None = None  # 1 要素 = 1 チャンク（/execute は 1 行 = 1 イベント）
    execution: ExecutionHandle | None = None  # run_code の未完了実行（HTTP 層が待機・監視）

    @classmethod
    def json(cls, status: int, payload: object, headers: dict[str, str] | None = None) -> Response:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return cls(status=status, body=data, headers=dict(headers or {}))

    @classmethod
    def error(cls, status: int, message: str) -> Response:
        """E2B エラー形 ``{"code", "message"}``（401→AuthenticationException、
        404→SandboxNotFoundException 等に写像される——spike §1.1）。"""
        return cls.json(status, {"code": status, "message": message})


class ShimApp:
    """認証・default-deny enforce・ドライバ結線を担うディスパッチャ（ソケット非依存）。"""

    def __init__(
        self,
        *,
        driver: Driver,
        api_key: str,
        allow_shared_kernel: bool,
        default_template_id: str | None = None,
    ) -> None:
        self.driver = driver
        self._api_key = api_key
        self.allow_shared_kernel = allow_shared_kernel
        self.default_template_id = default_template_id
        # データプレーン TLS リスナーの実ポート（lifecycle が bind 後に設定。
        # create 応答の domain "sbx.localhost:{port}" に埋め込む）。
        self.data_port: int | None = None
        # サンドボックス単位の envd アクセストークン（create 時に発行、X-Access-Token で検証）。
        self._envd_tokens: dict[str, str] = {}
        # endAt 算出用（create 時の timeout 秒を保存）。
        self._end_at: dict[str, float] = {}
        # アイドルタイムアウト判定用（lifecycle が監視する）。
        self.last_activity: float = monotonic()

    # --- 状態参照 ---------------------------------------------------------

    def active_sandboxes(self) -> list[str]:
        """現在 envd トークンを保持する（= 生存中の）サンドボックス id 一覧。

        lifecycle がシャットダウン時にこれらを destroy してネットワーク残骸を掃除する。
        """
        return list(self._envd_tokens)

    # --- 認証 ------------------------------------------------------------

    def _check_control_auth(self, req: Request) -> bool:
        """制御プレーン: X-API-KEY を定時間比較で検証。"""
        provided = req.header(wire.HEADER_API_KEY)
        return bool(provided) and hmac.compare_digest(provided, self._api_key)

    def _check_envd_auth(self, req: Request, sandbox_id: str) -> bool:
        """データプレーン: サンドボックスの X-Access-Token を検証（sandbox_id との対応込み）。"""
        expected = self._envd_tokens.get(sandbox_id)
        provided = req.header(wire.HEADER_ACCESS_TOKEN)
        return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)

    # --- ディスパッチ（制御プレーン） ------------------------------------

    def dispatch_control(self, req: Request) -> Response:
        """制御プレーン要求を解決・認証してドライバへ委譲する。"""
        self.last_activity = monotonic()
        try:
            route = resolve_control(req.method, req.path)
        except UnknownRoute:
            return Response.error(404, "not found")

        if not self._check_control_auth(req):
            _log.warning("auth_denied plane=control op=%s", route.operation.value)
            return Response.error(401, "missing or invalid API key")

        handler = self._CONTROL_HANDLERS[route.operation]
        try:
            return handler(self, req, route)
        except KeyError:
            # ドライバの「未知 sandbox_id」（Mock は KeyError 派生）は 404 に写像する。
            return Response.error(404, f"sandbox not found: {route.params.get('sandbox_id', '')}")
        except Exception as exc:  # ドライバ失敗（診断ログ点）。
            return self._driver_failure(route, exc)

    # --- ディスパッチ（データプレーン） ----------------------------------

    def dispatch_data(self, req: Request) -> Response:
        """データプレーン要求を Host ルーティング・認証してドライバへ委譲する。"""
        self.last_activity = monotonic()
        parsed = wire.parse_subdomain_host(req.header("Host"))
        if parsed is None:
            return Response.error(404, "unknown host (expected {port}-{sandbox_id}.sbx.localhost)")
        subdomain_port, sandbox_id = parsed
        try:
            route = resolve_data(subdomain_port, req.method, req.path)
        except UnknownRoute:
            return Response.error(404, "not found")

        if not self._check_envd_auth(req, sandbox_id):
            _log.warning(
                "auth_denied plane=envd op=%s sandbox_id=%s", route.operation.value, sandbox_id
            )
            return Response.error(401, "missing or invalid access token")

        handler = self._DATA_HANDLERS[route.operation]
        try:
            return handler(self, req, sandbox_id)
        except KeyError:
            return Response.error(404, "not found")
        except Exception as exc:
            return self._driver_failure(route, exc)

    def _driver_failure(self, route: Route, exc: Exception) -> Response:
        """ドライバ例外の診断ログと 500 応答（egress 遮断下の接続失敗も本経路で記録）。"""
        _log.error("driver_call_failed op=%s error=%s", route.operation.value, type(exc).__name__)
        if isinstance(exc, ConnectionError | TimeoutError | OSError):
            _log.error("dataplane_connect_failed op=%s detail=%s", route.operation.value, exc)
        return Response.error(500, f"driver error: {type(exc).__name__}")

    # --- 制御プレーンハンドラ --------------------------------------------

    def _handle_create(self, req: Request, route: Route) -> Response:
        payload = _parse_json(req.body)
        # SDK 実送信は camelCase の templateID（spike §1.1）。旧 snake_case も受ける。
        template_id = (
            payload.get("templateID") or payload.get("template_id") or self.default_template_id
        )
        if not template_id:
            return Response.error(400, "missing templateID")
        metadata = payload.get("metadata") or {}

        # default-deny enforce。ローカルドライバの隔離レベルは必ず 3 値。
        decision = route_execution(
            self.driver.isolation_level, allow_shared_kernel=self.allow_shared_kernel
        )
        if not decision.allowed:
            _log.warning("execution_denied level=%s reason=%s", decision.level, decision.reason)
            return Response.json(
                403,
                {"code": 403, "message": f"execution denied: {decision.reason}"},
                headers={wire.HEADER_ISOLATION_LEVEL: str(decision.level)},
            )

        if self.data_port is None:
            # データプレーン未起動では SDK の run_code/files が成立しない（設定バグの早期検出）。
            _log.error("create_rejected reason=data-plane-not-bound")
            return Response.error(500, "data plane not bound")

        info = self.driver.create(template_id=str(template_id), metadata=dict(metadata))
        # envd アクセストークンを発行し保存（X-Access-Token 検証の基準。secure 値に
        # 関係なく常に返す——spike §3）。
        access = secrets.token_urlsafe(32)
        self._envd_tokens[info.sandbox_id] = access
        try:
            timeout = float(payload.get("timeout") or _DEFAULT_SANDBOX_TIMEOUT)
        except (TypeError, ValueError):
            timeout = _DEFAULT_SANDBOX_TIMEOUT
        self._end_at[info.sandbox_id] = info.started_at + timeout
        # Sandbox モデル必須キー: clientID・envdVersion・sandboxID・templateID（欠落は
        # SDK クラッシュ）。domain は必ず返す（未返却は e2b.app へフォールバック——spike §6-5）。
        body = {
            "sandboxID": info.sandbox_id,
            "clientID": wire.CLIENT_ID,
            "templateID": info.template_id,
            "envdVersion": wire.ENVD_VERSION,
            "envdAccessToken": access,
            "domain": wire.build_domain(self.data_port),
        }
        return Response.json(
            201, body, headers={wire.HEADER_ISOLATION_LEVEL: str(info.isolation_level)}
        )

    def _handle_info(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        if sid not in self._envd_tokens:
            # destroy 済み・未知はドライバに聞かず 404（kill 後の get_info の正常経路）。
            return Response.error(404, f"sandbox not found: {sid}")
        info = self.driver.get_info(sid)
        # SandboxDetail 必須 10 キー（spike §1.1）。metadata はそのまま SandboxInfo.metadata へ
        # round-trip し、隔離レベルの正典（ISOLATION_LEVEL_KEY）を運ぶ。
        end_at = self._end_at.get(sid, info.started_at + _DEFAULT_SANDBOX_TIMEOUT)
        body = {
            "sandboxID": info.sandbox_id,
            "clientID": wire.CLIENT_ID,
            "templateID": info.template_id,
            "envdVersion": wire.ENVD_VERSION,
            "cpuCount": _NOMINAL_CPU_COUNT,
            "memoryMB": _NOMINAL_MEMORY_MB,
            "diskSizeMB": _NOMINAL_DISK_MB,
            "startedAt": _rfc3339(info.started_at),
            "endAt": _rfc3339(end_at),
            "state": "running",
            "metadata": dict(info.metadata),
        }
        return Response.json(
            200, body, headers={wire.HEADER_ISOLATION_LEVEL: str(info.isolation_level)}
        )

    def _handle_destroy(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        if sid not in self._envd_tokens:
            # 404 → SDK は kill()==False（例外なし——spike §1.1）。
            return Response.error(404, f"sandbox not found: {sid}")
        self.driver.destroy(sid)  # ネットワーク残骸の掃除はドライバ責務。
        self._envd_tokens.pop(sid, None)
        self._end_at.pop(sid, None)
        return Response(status=204)

    # --- データプレーンハンドラ（sandbox_id は Host 由来） ----------------

    def _handle_health(self, req: Request, sandbox_id: str) -> Response:
        # 2xx = envd 稼働（run_code の接続断エラー時に SDK が自動で叩く——spike §1.2）。
        return Response.json(200, {})

    def _handle_file_read(self, req: Request, sandbox_id: str) -> Response:
        path = req.query.get("path")
        if not path:
            return Response.error(400, "missing path")
        try:
            data = self.driver.get_file(sandbox_id, path)
        except KeyError:
            return Response.error(404, f"file not found: {path}")
        return Response(status=200, body=data, content_type="application/octet-stream")

    def _handle_file_write(self, req: Request, sandbox_id: str) -> Response:
        # 既定 multipart/form-data（part 名 file、filename=パス）。1 件時のみ path クエリが
        # 付く（spike §1.2）。応答は非空 JSON 配列（空/非配列は SandboxException）。
        content_type = req.header("Content-Type") or ""
        if "multipart" not in content_type:
            return Response.error(400, "expected multipart/form-data")
        entries: list[dict[str, str]] = []
        for filename, data in _iter_multipart_files(content_type, req.body):
            path = req.query.get("path") or filename
            if not path:
                return Response.error(400, "missing path")
            self.driver.put_file(sandbox_id, path, data)
            name = path.rsplit("/", 1)[-1]
            entries.append({"name": name, "type": "file", "path": path})
        if not entries:
            return Response.error(400, "no file parts")
        return Response.json(200, entries)

    def _handle_run_code(self, req: Request, sandbox_id: str) -> Response:
        payload = _parse_json(req.body)
        code = payload.get("code")
        if code is None:
            return Response.error(400, "missing code")
        # キャンセル可能なハンドルで実行を開始し、完了待機と切断監視は HTTP 層へ委ねる
        # （クライアント TCP 切断 = 実行キャンセル）。非 2xx はストリーム前判定のため、
        # イベント配信はハンドル完了後に始まる。
        handle = self.driver.exec_start(sandbox_id, str(code))
        return Response(status=200, content_type="application/json", execution=handle)

    # 操作 → ハンドラ表。
    _CONTROL_HANDLERS = {
        wire.Operation.SANDBOX_CREATE: _handle_create,
        wire.Operation.SANDBOX_INFO: _handle_info,
        wire.Operation.SANDBOX_DESTROY: _handle_destroy,
    }
    _DATA_HANDLERS = {
        wire.Operation.HEALTH: _handle_health,
        wire.Operation.FILE_READ: _handle_file_read,
        wire.Operation.FILE_WRITE: _handle_file_write,
        wire.Operation.RUN_CODE: _handle_run_code,
    }


def _parse_json(body: bytes) -> dict:
    """JSON ボディを dict へ。空・不正は空 dict（ハンドラ側で必須項目を検査）。"""
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_multipart_files(content_type: str, body: bytes) -> Iterator[tuple[str | None, bytes]]:
    """multipart/form-data から (filename, データ) を順に取り出す（stdlib email パーサ）。"""
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    for part in msg.iter_parts():
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        yield part.get_filename(), payload


def _execution_event_lines(execution: Execution) -> Iterator[bytes]:
    """:class:`Execution` を /execute の JSON lines イベント列へ逆変換する。

    1 イベント = 1 行厳守（空行は SDK クラッシュ）。stdout/stderr は timestamp(ns) 必須。
    終端はボディ終端のみ（番兵なし）——spike §1.3。
    """
    events: list[dict[str, object]] = []
    for text in execution.logs.stdout:
        events.append({"type": "stdout", "text": text, "timestamp": time.time_ns()})
    for text in execution.logs.stderr:
        events.append({"type": "stderr", "text": text, "timestamp": time.time_ns()})
    for result in execution.results:
        ev: dict[str, object] = {"type": "result", "is_main_result": result.is_main_result}
        if result.text is not None:
            ev["text"] = result.text
        events.append(ev)
    if execution.error is not None:
        events.append(
            {
                "type": "error",
                "name": execution.error.name,
                "value": execution.error.value,
                "traceback": execution.error.traceback,
            }
        )
    if execution.execution_count is not None:
        events.append(
            {"type": "number_of_executions", "execution_count": execution.execution_count}
        )
    for ev in events:
        yield json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n"


# --- HTTP バインド（127.0.0.1 限定） -----------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """ThreadingHTTPServer 用ハンドラ。プレーンに応じ :class:`ShimApp` へ委譲。"""

    # chunked 転送（/execute）に HTTP/1.1 が必須（spike §1.3）。
    protocol_version = "HTTP/1.1"

    # BaseHTTPRequestHandler の既定ログ（stderr へ 1 行）を抑止（診断は logging 経由に統一）。
    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _await_execution(self, handle: ExecutionHandle) -> Execution | None:
        """実行完了を待ちつつクライアント切断を監視する。切断時は cancel して None。

        HTTP/1.1 ではクライアントが応答前に追加データを送ることはないため、ソケットの
        readable 化はほぼ切断（EOF / TLS close_notify）を意味する。recv が b"" なら切断。
        TLS レコード不完全（SSLWantReadError）は継続する。
        """
        sock = self.connection
        while True:
            if handle.done():
                return handle.result()
            try:
                readable, _, _ = select.select([sock], [], [], 0.05)
            except (OSError, ValueError):  # ソケットが既に閉じられた。
                readable = [sock]
            if not readable:
                continue
            try:
                data = sock.recv(1)
            except ssl.SSLWantReadError:
                continue
            except OSError:
                data = b""
            if data == b"":
                _log.info("run_code_client_disconnected_cancelling")
                handle.cancel()
                # ハンドルの後始末（結果は捨てる。例外もここで畳む）。
                with contextlib.suppress(Exception):
                    handle.result()
                return None
            # 想定外の追加データは読み捨てて継続（応答前のパイプラインは SDK に無い）。

    def _serve(self) -> None:
        server: ShimHTTPServer = self.server  # type: ignore[assignment]
        req = Request.from_raw(
            self.command, self.path, dict(self.headers.items()), self._read_body()
        )
        if server.plane == "data":
            resp = server.app.dispatch_data(req)
        else:
            resp = server.app.dispatch_control(req)

        if resp.execution is not None:
            # run_code: 完了待機 + 切断監視（切断 = キャンセル。応答は送らない）。
            try:
                execution = self._await_execution(resp.execution)
            except Exception as exc:  # ドライバ失敗はストリーム前判定の 500 に写像。
                _log.error("driver_call_failed op=run_code error=%s", type(exc).__name__)
                resp = Response.error(500, f"driver error: {type(exc).__name__}")
            else:
                if execution is None:
                    self.close_connection = True
                    return
                resp = Response(
                    status=200,
                    content_type="application/json",
                    stream=_execution_event_lines(execution),
                )

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        if resp.stream is not None:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in resp.stream:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                # クライアント TCP 切断 = 実行キャンセル（v0 は一括実行済みのため配信中断のみ）。
                _log.info("stream_client_disconnected")
            return
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    # 必要な 3 メソッド（制御: POST/GET/DELETE、データ: GET/POST）。
    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve


class ShimHTTPServer(ThreadingHTTPServer):
    """アプリ参照とプレーン種別を保持する ThreadingHTTPServer。"""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app: ShimApp, plane: str) -> None:
        super().__init__(server_address, _Handler)
        self.app = app
        self.plane = plane

    def handle_error(self, request: object, client_address: object) -> None:
        """接続系例外（クライアント切断等）は生 traceback を出さず logging に流す。"""
        exc = sys.exc_info()[1]  # sys.exception() は 3.12+（requires-python は 3.11）。
        if isinstance(exc, BrokenPipeError | ConnectionResetError | ssl.SSLError | TimeoutError):
            _log.info("client_connection_error type=%s", type(exc).__name__)
            return
        super().handle_error(request, client_address)


def _is_loopback(host: str) -> bool:
    """host がループバックか（127.0.0.1 限定 bind の防御）。"""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return socket.inet_aton(host).startswith(b"\x7f")  # 127.0.0.0/8
    except OSError:
        return False


def make_server(
    app: ShimApp,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    plane: str = "control",
    ssl_context: ssl.SSLContext | None = None,
) -> ShimHTTPServer:
    """ループバックに bind した HTTP サーバーを返す（port=0 で OS が空きポートを付与）。

    - ``plane="control"``: 平文 HTTP。実ポートは ``.cube/port`` へ（lifecycle）。
    - ``plane="data"`` + ``ssl_context``: 単一 TLS リスナー（``*.sbx.localhost`` 証明書・
      ALPN h2 非広告）。実ポートは create 応答の domain に埋め込む（``ShimApp.data_port``）。

    ループバック以外の host は :class:`ValueError`（127.0.0.1 限定 bind）。
    実ポートは ``server.server_address[1]`` で取得できる。
    """
    if not _is_loopback(host):
        raise ValueError(f"シムは 127.0.0.1（ループバック）にのみ bind します: {host!r}")
    server = ShimHTTPServer((host, port), app, plane)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server
