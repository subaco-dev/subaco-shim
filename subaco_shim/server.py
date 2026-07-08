"""ローカル HTTP シムサーバー（127.0.0.1 限定 bind）。

責務:

- **127.0.0.1 限定 bind**。ループバック以外へは bind しない。
- **トークン検証**: 制御プレーン = ``X-API-KEY``、envd データプレーン =
  ``X-Access-Token``。なし／不一致は両経路で **401**。
- **default-deny enforce**: create 時に実行先ドライバの隔離レベルで
  :func:`~subaco_shim.isolation.route_execution` を評価。shared-kernel は
  ``allow_shared_kernel`` オプトイン時のみ許可、unknown は無条件拒否、vm-per-container 以上は
  無条件許可。**エージェント申告の trust では緩和しない**（route_execution は trust を取らない）。
  拒否は **403**。
- **ドライバへのルーティング**（:mod:`subaco_shim.protocol`）と、get_info の metadata に
  ``isolation_level`` を載せて返却、デバッグ用 ``X-Isolation-Level`` ヘッダも付す。

**この HTTP/JSON API は E2B ワイヤの忠実再現ではない**（忠実再現は将来の spike。
:mod:`subaco_shim.protocol.wire` の TODO 参照）。認証・enforce・ドライバ結線を実配線・検証
可能にするための骨子である。

:class:`ShimApp.dispatch` は純関数的（ソケット非依存）でユニットテスト可能。HTTP バインドは
:func:`make_server`（ループバック強制）で行う。stdlib のみに依存する。
"""

from __future__ import annotations

import hmac
import json
import secrets
import socket
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from .drivers.base import Driver
from .isolation import route_execution
from .logging import get_logger
from .models import ISOLATION_LEVEL_KEY
from .protocol import wire
from .protocol.routes import Route, UnknownRoute, resolve

_log = get_logger("server")

# ループバック以外への bind を拒否するための許可ホスト（127.0.0.1 限定 bind）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


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
    """ディスパッチ出力。"""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"

    @classmethod
    def json(cls, status: int, payload: object, headers: dict[str, str] | None = None) -> Response:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return cls(status=status, body=data, headers=dict(headers or {}))

    @classmethod
    def error(cls, status: int, error: str, reason: str | None = None) -> Response:
        payload: dict[str, object] = {"error": error}
        if reason is not None:
            payload["reason"] = reason
        return cls.json(status, payload)


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
        # サンドボックス単位の envd アクセストークン（create 時に発行、X-Access-Token で検証）。
        self._envd_tokens: dict[str, str] = {}
        # アイドルタイムアウト判定用（lifecycle が監視する）。
        self.last_activity: float = monotonic()

    # --- 認証 ------------------------------------------------------------

    def active_sandboxes(self) -> list[str]:
        """現在 envd トークンを保持する（= 生存中の）サンドボックス id 一覧。

        lifecycle がシャットダウン時にこれらを destroy してネットワーク残骸を掃除する。
        """
        return list(self._envd_tokens)

    def _check_control_auth(self, req: Request) -> bool:
        """制御プレーン: X-API-KEY を定時間比較で検証。"""
        provided = req.header(wire.HEADER_API_KEY)
        return bool(provided) and hmac.compare_digest(provided, self._api_key)

    def _check_envd_auth(self, req: Request, sandbox_id: str) -> bool:
        """envd データプレーン: サンドボックスの X-Access-Token を検証。"""
        expected = self._envd_tokens.get(sandbox_id)
        provided = req.header(wire.HEADER_ACCESS_TOKEN)
        return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)

    # --- ディスパッチ ----------------------------------------------------

    def dispatch(self, req: Request) -> Response:
        """要求を解決・認証・enforce してドライバへ委譲する。"""
        self.last_activity = monotonic()
        try:
            route = resolve(req.method, req.path)
        except UnknownRoute:
            return Response.error(404, "not-found")

        plane = wire.OPERATION_PLANE[route.operation]
        if plane is wire.Plane.CONTROL:
            if not self._check_control_auth(req):
                _log.warning("auth_denied plane=control op=%s", route.operation.value)
                return Response.error(401, "unauthorized", "missing-or-invalid-api-key")
        else:  # envd
            sid = route.params.get("sandbox_id", "")
            if not self._check_envd_auth(req, sid):
                _log.warning("auth_denied plane=envd op=%s", route.operation.value)
                return Response.error(401, "unauthorized", "missing-or-invalid-access-token")

        handler = self._HANDLERS[route.operation]
        try:
            return handler(self, req, route)
        except Exception as exc:  # ドライバ失敗（診断ログ点）。
            _log.error(
                "driver_call_failed op=%s error=%s", route.operation.value, type(exc).__name__
            )
            # egress 遮断下のデータプレーン接続失敗も本経路で記録する。
            if _looks_like_connect_failure(exc):
                _log.error("dataplane_connect_failed op=%s detail=%s", route.operation.value, exc)
            return Response.error(500, "driver-error", type(exc).__name__)

    # --- 各操作ハンドラ --------------------------------------------------

    def _handle_create(self, req: Request, route: Route) -> Response:
        payload = _parse_json(req.body)
        template_id = payload.get("template_id") or self.default_template_id
        if not template_id:
            return Response.error(400, "bad-request", "missing-template-id")
        metadata = payload.get("metadata") or {}

        # default-deny enforce。ローカルドライバの隔離レベルは必ず 3 値。
        decision = route_execution(
            self.driver.isolation_level, allow_shared_kernel=self.allow_shared_kernel
        )
        if not decision.allowed:
            _log.warning("execution_denied level=%s reason=%s", decision.level, decision.reason)
            return Response.json(
                403,
                {
                    "error": "execution-denied",
                    "reason": decision.reason,
                    ISOLATION_LEVEL_KEY: str(decision.level),
                },
                headers={wire.HEADER_ISOLATION_LEVEL: str(decision.level)},
            )

        info = self.driver.create(template_id=str(template_id), metadata=dict(metadata))
        # envd アクセストークンを発行し保存（X-Access-Token 検証の基準）。
        access = secrets.token_urlsafe(32)
        self._envd_tokens[info.sandbox_id] = access
        body = {
            "sandbox_id": info.sandbox_id,
            "template_id": info.template_id,
            "envd_access_token": access,
            "metadata": info.metadata,
            # TODO: sandbox_domain のローカル解決は spike で確定（骨子は同一 origin 経路）。
            "sandbox_domain": None,
        }
        return Response.json(
            201, body, headers={wire.HEADER_ISOLATION_LEVEL: str(info.isolation_level)}
        )

    def _handle_info(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        info = self.driver.get_info(sid)
        # 隔離レベルの正典は metadata。X-Isolation-Level はデバッグ用補助。
        return Response.json(
            200, info.to_dict(), headers={wire.HEADER_ISOLATION_LEVEL: str(info.isolation_level)}
        )

    def _handle_destroy(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        self.driver.destroy(sid)  # ネットワーク残骸の掃除はドライバ責務。
        self._envd_tokens.pop(sid, None)
        return Response.json(200, {"sandbox_id": sid, "destroyed": True})

    def _handle_run_code(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        payload = _parse_json(req.body)
        code = payload.get("code")
        if code is None:
            return Response.error(400, "bad-request", "missing-code")
        execution = self.driver.exec(sid, str(code))
        # E2B Execution 互換の構造化出力（.text / .to_json）。
        return Response.json(200, execution.to_dict())

    def _handle_file_write(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        path = req.query.get("path")
        if not path:
            return Response.error(400, "bad-request", "missing-path")
        self.driver.put_file(sid, path, req.body)
        return Response.json(200, {"path": path, "written": len(req.body)})

    def _handle_file_read(self, req: Request, route: Route) -> Response:
        sid = route.params["sandbox_id"]
        path = req.query.get("path")
        if not path:
            return Response.error(400, "bad-request", "missing-path")
        data = self.driver.get_file(sid, path)
        return Response(status=200, body=data, content_type="application/octet-stream")

    # 操作 → ハンドラ表。
    _HANDLERS = {
        wire.Operation.SANDBOX_CREATE: _handle_create,
        wire.Operation.SANDBOX_INFO: _handle_info,
        wire.Operation.SANDBOX_DESTROY: _handle_destroy,
        wire.Operation.RUN_CODE: _handle_run_code,
        wire.Operation.FILE_WRITE: _handle_file_write,
        wire.Operation.FILE_READ: _handle_file_read,
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


def _looks_like_connect_failure(exc: Exception) -> bool:
    """例外が接続失敗系か（egress 遮断下のデータプレーン到達失敗の診断ログ点）。"""
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


# --- HTTP バインド（127.0.0.1 限定） -----------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """ThreadingHTTPServer 用ハンドラ。すべての要求を :meth:`ShimApp.dispatch` へ委譲。"""

    # BaseHTTPRequestHandler の既定ログ（stderr へ 1 行）を抑止（診断は logging 経由に統一）。
    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _serve(self) -> None:
        app: ShimApp = self.server.app  # type: ignore[attr-defined]
        req = Request.from_raw(
            self.command, self.path, dict(self.headers.items()), self._read_body()
        )
        resp = app.dispatch(req)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    # 5 系統に必要な 3 メソッド。
    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve


class ShimHTTPServer(ThreadingHTTPServer):
    """アプリ参照を保持する ThreadingHTTPServer。"""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app: ShimApp) -> None:
        super().__init__(server_address, _Handler)
        self.app = app


def _is_loopback(host: str) -> bool:
    """host がループバックか（127.0.0.1 限定 bind の防御）。"""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return socket.inet_aton(host).startswith(b"\x7f")  # 127.0.0.0/8
    except OSError:
        return False


def make_server(app: ShimApp, *, host: str = "127.0.0.1", port: int = 0) -> ShimHTTPServer:
    """ループバックに bind した HTTP サーバーを返す（port=0 で OS が空きポートを付与）。

    ループバック以外の host は :class:`ValueError`（127.0.0.1 限定 bind）。
    実ポートは ``server.server_address[1]`` で取得できる（lifecycle が .cube/port へ書く）。
    """
    if not _is_loopback(host):
        raise ValueError(f"シムは 127.0.0.1（ループバック）にのみ bind します: {host!r}")
    return ShimHTTPServer((host, port), app)
