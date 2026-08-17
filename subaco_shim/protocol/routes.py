"""E2B ワイヤのルート解決（制御プレーン / データプレーンの 2 面）。

spike（docs/00-memo/05_spike結果_E2B_ワイヤ.md）で実測確定した E2B ワイヤ面:

制御プレーン（``E2B_API_URL`` の平文 HTTP・``X-API-KEY``）:

    POST   /sandboxes          → SANDBOX_CREATE  (201)
    GET    /sandboxes/{id}     → SANDBOX_INFO    (200, SandboxDetail 必須 10 キー + metadata)
    DELETE /sandboxes/{id}     → SANDBOX_DESTROY (204 / 404)

データプレーン（単一 TLS リスナー・Host ヘッダ ``{port}-{sandbox_id}.sbx.localhost`` で
ルーティング・``X-Access-Token``）。sandbox_id はパスではなく Host から得る:

    ENVD_PORT(49983) 面:   GET  /health → HEALTH / GET /files → FILE_READ /
                           POST /files → FILE_WRITE（multipart）
    RUN_CODE_PORT(49999) 面: POST /execute → RUN_CODE（chunked JSON lines 応答）

未実装エンドポイント（/timeout・/pause・/snapshots・/metrics・/v2 等）は 404 で可
（spike §1.1——SDK の ``with`` 終了は kill のみ）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .wire import ENVD_PORT, RUN_CODE_PORT, Operation

# サンドボックス id の許容文字（driver は hex を生成するが、余裕を持たせる）。
_SID = r"(?P<sandbox_id>[A-Za-z0-9_-]+)"

# 制御プレーン: (HTTP メソッド, コンパイル済みパス正規表現) → 操作。定義順に評価する。
_CONTROL_ROUTES: list[tuple[str, re.Pattern[str], Operation]] = [
    ("POST", re.compile(r"^/sandboxes/?$"), Operation.SANDBOX_CREATE),
    ("GET", re.compile(rf"^/sandboxes/{_SID}/?$"), Operation.SANDBOX_INFO),
    ("DELETE", re.compile(rf"^/sandboxes/{_SID}/?$"), Operation.SANDBOX_DESTROY),
]

# データプレーン: サブドメインの {port} → (メソッド, パス) → 操作。
_DATA_ROUTES: dict[int, list[tuple[str, str, Operation]]] = {
    ENVD_PORT: [
        ("GET", "/health", Operation.HEALTH),
        ("GET", "/files", Operation.FILE_READ),
        ("POST", "/files", Operation.FILE_WRITE),
    ],
    RUN_CODE_PORT: [
        ("POST", "/execute", Operation.RUN_CODE),
    ],
}


class UnknownRoute(LookupError):
    """既知のルートに一致しない（404 相当）。"""


@dataclass(frozen=True)
class Route:
    """解決したルート（操作とパスパラメータ）。"""

    operation: Operation
    params: dict[str, str] = field(default_factory=dict)


def resolve_control(method: str, path: str) -> Route:
    """制御プレーンの (method, path) を :class:`Route` へ解決する。

    ``path`` はクエリ文字列を含まないパス部分（呼び出し側で分離済み）を渡す。
    一致しなければ :class:`UnknownRoute`（404 ``{"code","message"}`` で応答する）。
    """
    method = method.upper()
    for m, pattern, op in _CONTROL_ROUTES:
        if m != method:
            continue
        match = pattern.match(path)
        if match:
            return Route(operation=op, params=dict(match.groupdict()))
    raise UnknownRoute(f"{method} {path}")


def resolve_data(subdomain_port: int, method: str, path: str) -> Route:
    """データプレーンの (サブドメイン {port}, method, path) を :class:`Route` へ解決する。

    sandbox_id は Host ヘッダ由来のため params には載せない（呼び出し側が
    :func:`~subaco_shim.protocol.wire.parse_subdomain_host` の結果を併用する）。
    """
    method = method.upper()
    normalized = path.rstrip("/") or "/"
    for m, p, op in _DATA_ROUTES.get(subdomain_port, []):
        if m == method and p == normalized:
            return Route(operation=op)
    raise UnknownRoute(f"{subdomain_port}: {method} {path}")
