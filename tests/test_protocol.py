"""protocol 層のルート解決・プレーン／認証マッピング（骨子）。"""

from __future__ import annotations

import pytest

from subaco_shim.protocol import (
    HEADER_ACCESS_TOKEN,
    HEADER_API_KEY,
    Operation,
    Plane,
    UnknownRoute,
    auth_header_for,
    resolve,
    wire,
)


def test_ports_and_headers():
    # envd=49983 / run_code=49999。
    assert wire.ENVD_PORT == 49983
    assert wire.RUN_CODE_PORT == 49999
    assert HEADER_API_KEY == "X-API-KEY"
    assert HEADER_ACCESS_TOKEN == "X-Access-Token"


def test_route_resolution():
    assert resolve("POST", "/v0/sandboxes").operation is Operation.SANDBOX_CREATE
    r = resolve("GET", "/v0/sandboxes/abc123")
    assert r.operation is Operation.SANDBOX_INFO
    assert r.params["sandbox_id"] == "abc123"
    assert resolve("DELETE", "/v0/sandboxes/x").operation is Operation.SANDBOX_DESTROY
    assert resolve("POST", "/v0/sandboxes/x/run_code").operation is Operation.RUN_CODE
    assert resolve("POST", "/v0/sandboxes/x/files").operation is Operation.FILE_WRITE
    assert resolve("GET", "/v0/sandboxes/x/files").operation is Operation.FILE_READ


def test_unknown_route():
    with pytest.raises(UnknownRoute):
        resolve("GET", "/v0/unknown")
    with pytest.raises(UnknownRoute):
        resolve("PUT", "/v0/sandboxes/x")  # 未対応メソッド


def test_plane_and_auth_mapping():
    # 制御プレーンは X-API-KEY、envd は X-Access-Token。
    assert wire.OPERATION_PLANE[Operation.SANDBOX_CREATE] is Plane.CONTROL
    assert wire.OPERATION_PLANE[Operation.RUN_CODE] is Plane.ENVD
    assert auth_header_for(Operation.SANDBOX_INFO) == HEADER_API_KEY
    assert auth_header_for(Operation.FILE_READ) == HEADER_ACCESS_TOKEN
    assert auth_header_for(Operation.FILE_WRITE) == HEADER_ACCESS_TOKEN
