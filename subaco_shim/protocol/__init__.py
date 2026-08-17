"""E2B ワイヤプロトコル層（spike 実測確定済みの実配線仕様）。

- :mod:`subaco_shim.protocol.wire`   — プレーン／操作／認証ヘッダ／データプレーンポート／
  サブドメイン解決（``parse_subdomain_host`` / ``build_domain``）。
- :mod:`subaco_shim.protocol.routes` — 制御プレーン（E2B 形 3 ルート）と
  データプレーン（envd / run_code のホストベース面）のルート解決。

仕様の根拠は docs/00-memo/05_spike結果_E2B_ワイヤ.md（判定 full-fidelity-feasible、
固定 e2b==2.30.0 / e2b-code-interpreter==2.8.1）。stdlib のみに依存する。
"""

from __future__ import annotations

from .routes import Route, UnknownRoute, resolve_control, resolve_data
from .wire import (
    ENVD_PORT,
    ENVD_VERSION,
    HEADER_ACCESS_TOKEN,
    HEADER_API_KEY,
    HEADER_ISOLATION_LEVEL,
    HEADER_SANDBOX_ID,
    OPERATION_PLANE,
    PLANE_AUTH_HEADER,
    RUN_CODE_PORT,
    SANDBOX_DOMAIN_BASE,
    Operation,
    Plane,
    auth_header_for,
    build_domain,
    parse_subdomain_host,
)

__all__ = [
    "ENVD_PORT",
    "ENVD_VERSION",
    "HEADER_ACCESS_TOKEN",
    "HEADER_API_KEY",
    "HEADER_ISOLATION_LEVEL",
    "HEADER_SANDBOX_ID",
    "OPERATION_PLANE",
    "PLANE_AUTH_HEADER",
    "RUN_CODE_PORT",
    "SANDBOX_DOMAIN_BASE",
    "Operation",
    "Plane",
    "Route",
    "UnknownRoute",
    "auth_header_for",
    "build_domain",
    "parse_subdomain_host",
    "resolve_control",
    "resolve_data",
]
