"""protocol 層のルート解決・プレーン／認証マッピング・サブドメイン解決（E2B ワイヤ形）。"""

from __future__ import annotations

import pytest

from subaco_shim.protocol import (
    HEADER_ACCESS_TOKEN,
    HEADER_API_KEY,
    Operation,
    Plane,
    UnknownRoute,
    auth_header_for,
    resolve_control,
    resolve_data,
    wire,
)


def test_ports_and_headers():
    # envd=49983 / run_code=49999。
    assert wire.ENVD_PORT == 49983
    assert wire.RUN_CODE_PORT == 49999
    assert HEADER_API_KEY == "X-API-KEY"
    assert HEADER_ACCESS_TOKEN == "X-Access-Token"
    assert wire.ENVD_VERSION == "0.5.5"


def test_control_route_resolution():
    # E2B 制御プレーン形（/v0 プレフィクスなし——spike §1.1）。
    assert resolve_control("POST", "/sandboxes").operation is Operation.SANDBOX_CREATE
    r = resolve_control("GET", "/sandboxes/abc123")
    assert r.operation is Operation.SANDBOX_INFO
    assert r.params["sandbox_id"] == "abc123"
    assert resolve_control("DELETE", "/sandboxes/x").operation is Operation.SANDBOX_DESTROY


def test_control_unknown_route():
    with pytest.raises(UnknownRoute):
        resolve_control("GET", "/v0/sandboxes/x")  # 旧 v0 形は廃止
    with pytest.raises(UnknownRoute):
        resolve_control("PUT", "/sandboxes/x")  # 未対応メソッド
    with pytest.raises(UnknownRoute):
        resolve_control("POST", "/sandboxes/x/timeout")  # 未実装エンドポイントは 404 で可


def test_data_route_resolution():
    # データプレーンはサブドメイン {port} 面ごとにルートが分かれる。
    assert resolve_data(wire.ENVD_PORT, "GET", "/health").operation is Operation.HEALTH
    assert resolve_data(wire.ENVD_PORT, "GET", "/files").operation is Operation.FILE_READ
    assert resolve_data(wire.ENVD_PORT, "POST", "/files").operation is Operation.FILE_WRITE
    assert resolve_data(wire.RUN_CODE_PORT, "POST", "/execute").operation is Operation.RUN_CODE


def test_data_unknown_route():
    with pytest.raises(UnknownRoute):
        resolve_data(wire.RUN_CODE_PORT, "GET", "/files")  # 面違い
    with pytest.raises(UnknownRoute):
        resolve_data(wire.ENVD_PORT, "POST", "/execute")  # 面違い
    with pytest.raises(UnknownRoute):
        resolve_data(12345, "GET", "/health")  # 未知ポート面


def test_subdomain_host_parsing():
    # spike 実測形: Host: 49999-sbx-tls-0001.sbx.localhost:8443
    assert wire.parse_subdomain_host("49999-sbx-tls-0001.sbx.localhost:8443") == (
        49999,
        "sbx-tls-0001",
    )
    # ポート部なし（既定 443 接続）でも解決できる。
    assert wire.parse_subdomain_host("49983-abc.sbx.localhost") == (49983, "abc")
    # sandbox_id 側のハイフンは維持される（最初の "-" のみが区切り）。
    assert wire.parse_subdomain_host("49983-a-b-c.sbx.localhost:1") == (49983, "a-b-c")
    # 不一致は None。
    assert wire.parse_subdomain_host(None) is None
    assert wire.parse_subdomain_host("example.com") is None
    assert wire.parse_subdomain_host("sbx.localhost:8443") is None
    assert wire.parse_subdomain_host("nodash.sbx.localhost") is None
    assert wire.parse_subdomain_host("x-abc.sbx.localhost") is None  # 非数値ポート


def test_build_domain():
    # ポート埋め込み domain（create 応答で必ず返す——spike §2(e)）。
    assert wire.build_domain(8443) == "sbx.localhost:8443"


def test_plane_and_auth_mapping():
    # 制御プレーンは X-API-KEY、envd/run_code は X-Access-Token。
    assert wire.OPERATION_PLANE[Operation.SANDBOX_CREATE] is Plane.CONTROL
    assert wire.OPERATION_PLANE[Operation.RUN_CODE] is Plane.ENVD
    assert wire.OPERATION_PLANE[Operation.HEALTH] is Plane.ENVD
    assert auth_header_for(Operation.SANDBOX_INFO) == HEADER_API_KEY
    assert auth_header_for(Operation.FILE_READ) == HEADER_ACCESS_TOKEN
    assert auth_header_for(Operation.FILE_WRITE) == HEADER_ACCESS_TOKEN
