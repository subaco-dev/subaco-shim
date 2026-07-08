"""E2B ワイヤプロトコル層（v0 骨子）。

- :mod:`subaco_shim.protocol.wire`   — プレーン／操作／認証ヘッダ／データプレーンポート定義。
- :mod:`subaco_shim.protocol.routes` — v0 ローカル JSON API のルート解決（spike 確定まで暫定）。

**忠実再現は最重要 spike**: envd（49983）は Connect RPC/protobuf 面、run_code（49999）の
トランスポート、サブドメイン形式・TLS・多重化は :mod:`.wire` の TODO で spike 参照を明記している。
stdlib のみに依存し、外部依存なしで import・動作する。
"""

from __future__ import annotations

from .routes import Route, UnknownRoute, resolve
from .wire import (
    HEADER_ACCESS_TOKEN,
    HEADER_API_KEY,
    HEADER_ISOLATION_LEVEL,
    OPERATION_PLANE,
    PLANE_AUTH_HEADER,
    Operation,
    Plane,
    auth_header_for,
)

__all__ = [
    "HEADER_ACCESS_TOKEN",
    "HEADER_API_KEY",
    "HEADER_ISOLATION_LEVEL",
    "OPERATION_PLANE",
    "PLANE_AUTH_HEADER",
    "Operation",
    "Plane",
    "Route",
    "UnknownRoute",
    "auth_header_for",
    "resolve",
]
