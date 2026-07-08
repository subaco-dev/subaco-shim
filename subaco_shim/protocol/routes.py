"""v0 ローカル JSON API のルート解決（骨子）。

**この経路は E2B ワイヤの忠実再現ではない。** 実 E2B は制御プレーンとサブドメイン形式の
envd/run_code データプレーン（Connect RPC/protobuf・多重化・TLS — :mod:`.wire` の TODO）で
構成されるが、その忠実再現は最重要 spike 事項である。本モジュールはそれが確定する
までの間、**同一の 5 系統操作**（:class:`~subaco_shim.protocol.wire.Operation`）を
127.0.0.1 上の単純な HTTP/JSON API へ写像し、認証・default-deny・ドライバ結線を実配線・検証
可能にするための薄い骨子である。

ルート（v0 ローカル JSON API）:

    POST   /v0/sandboxes                      → SANDBOX_CREATE  (control / X-API-KEY)
    GET    /v0/sandboxes/{id}                 → SANDBOX_INFO    (control / X-API-KEY)
    DELETE /v0/sandboxes/{id}                 → SANDBOX_DESTROY (control / X-API-KEY)
    POST   /v0/sandboxes/{id}/run_code        → RUN_CODE        (envd / X-Access-Token)
    POST   /v0/sandboxes/{id}/files           → FILE_WRITE      (envd / X-Access-Token)
    GET    /v0/sandboxes/{id}/files           → FILE_READ       (envd / X-Access-Token)

TODO: spike 確定後、サブドメイン形式（``{port}-{sandbox_id}.{sandbox_domain}``）の
ホストベースルーティングと envd の Connect RPC 面へ差し替える（:mod:`.wire` 参照）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .wire import Operation

# v0 ローカル JSON API のパス接頭辞。
API_PREFIX = "/v0"

# サンドボックス id の許容文字（driver は hex を生成するが、余裕を持たせる）。
_SID = r"(?P<sandbox_id>[A-Za-z0-9_-]+)"

# (HTTP メソッド, コンパイル済みパス正規表現) → 操作。定義順に評価する。
_ROUTES: list[tuple[str, re.Pattern[str], Operation]] = [
    ("POST", re.compile(rf"^{API_PREFIX}/sandboxes/?$"), Operation.SANDBOX_CREATE),
    (
        "GET",
        re.compile(rf"^{API_PREFIX}/sandboxes/{_SID}/?$"),
        Operation.SANDBOX_INFO,
    ),
    (
        "DELETE",
        re.compile(rf"^{API_PREFIX}/sandboxes/{_SID}/?$"),
        Operation.SANDBOX_DESTROY,
    ),
    (
        "POST",
        re.compile(rf"^{API_PREFIX}/sandboxes/{_SID}/run_code/?$"),
        Operation.RUN_CODE,
    ),
    (
        "POST",
        re.compile(rf"^{API_PREFIX}/sandboxes/{_SID}/files/?$"),
        Operation.FILE_WRITE,
    ),
    (
        "GET",
        re.compile(rf"^{API_PREFIX}/sandboxes/{_SID}/files/?$"),
        Operation.FILE_READ,
    ),
]


class UnknownRoute(LookupError):
    """既知のルートに一致しない（404 相当）。"""


@dataclass(frozen=True)
class Route:
    """解決したルート（操作とパスパラメータ）。"""

    operation: Operation
    params: dict[str, str]


def resolve(method: str, path: str) -> Route:
    """(method, path) を :class:`Route` へ解決する。一致しなければ :class:`UnknownRoute`。

    ``path`` はクエリ文字列を含まないパス部分（呼び出し側で分離済み）を渡す。
    """
    method = method.upper()
    for m, pattern, op in _ROUTES:
        if m != method:
            continue
        match = pattern.match(path)
        if match:
            return Route(operation=op, params=dict(match.groupdict()))
    raise UnknownRoute(f"{method} {path}")
