"""両プレーンのアクセス制御を実ソケットで検証する。

server を 127.0.0.1 の空きポートで実起動し、``http.client`` で HTTP 往復して次を確認する:

- **制御プレーン**（create / get_info / destroy）は ``X-API-KEY`` を要求し、なし／不一致は 401。
- **envd データプレーン**（run_code / files）はサンドボックス単位の ``X-Access-Token`` を要求し、
  なし／不一致は 401。
- **プレーン分離**: 制御プレーンの鍵で envd は通せず、envd トークンで制御プレーンは通せない
  （相互に認証を流用できない）。

MockDriver は ``vm-per-container``（default-deny で無条件許可）を宣言し、認証以外の理由
（隔離ルーティング拒否）で落ちないようにする。認証境界のみを純粋に検証する。
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from subaco_shim.drivers.mock import MockDriver
from subaco_shim.isolation import IsolationLevel
from subaco_shim.protocol import wire
from subaco_shim.server import ShimApp, make_server

API_KEY = "e2b_" + "a" * 32


@pytest.fixture
def live_server():
    """127.0.0.1 の空きポートで server を実起動し (port, app) を返す（後始末付き）。"""
    app = ShimApp(
        driver=MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER),
        api_key=API_KEY,
        allow_shared_kernel=False,
        default_template_id="tmpl@sha256:x",
    )
    server = make_server(app, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield port, app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(port, method, path, *, headers=None, body=None):
    """http.client で 1 往復し (status, body_bytes, headers_dict) を返す。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k: v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


def _create_sandbox(port):
    """正しい制御鍵でサンドボックスを 1 つ作り (sandbox_id, envd_access_token) を返す。"""
    status, body, _ = _call(
        port,
        "POST",
        "/v0/sandboxes",
        headers={wire.HEADER_API_KEY: API_KEY},
        body={"template_id": "tmpl"},
    )
    assert status == 201, body
    data = json.loads(body)
    return data["sandbox_id"], data["envd_access_token"]


# --- 制御プレーン: X-API-KEY ---------------------------------


def test_control_plane_missing_key_401(live_server):
    port, _ = live_server
    status, _, _ = _call(port, "POST", "/v0/sandboxes", body={"template_id": "t"})
    assert status == 401


def test_control_plane_wrong_key_401(live_server):
    port, _ = live_server
    status, _, _ = _call(
        port,
        "POST",
        "/v0/sandboxes",
        headers={wire.HEADER_API_KEY: "e2b_" + "9" * 32},
        body={"template_id": "t"},
    )
    assert status == 401


def test_control_plane_correct_key_201(live_server):
    port, _ = live_server
    status, body, _ = _call(
        port,
        "POST",
        "/v0/sandboxes",
        headers={wire.HEADER_API_KEY: API_KEY},
        body={"template_id": "t"},
    )
    assert status == 201
    data = json.loads(body)
    assert data["sandbox_id"]
    assert data["envd_access_token"]


def test_control_info_and_destroy_require_key(live_server):
    port, _ = live_server
    sid, _ = _create_sandbox(port)
    # get_info / destroy も制御プレーン。鍵なしは 401。
    assert _call(port, "GET", f"/v0/sandboxes/{sid}")[0] == 401
    assert _call(port, "DELETE", f"/v0/sandboxes/{sid}")[0] == 401
    # 正しい鍵なら 200。
    ok = _call(port, "GET", f"/v0/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    assert ok[0] == 200


# --- envd データプレーン: X-Access-Token（サンドボックス単位） ----------------------


def test_envd_plane_missing_token_401(live_server):
    port, _ = live_server
    sid, _ = _create_sandbox(port)
    status, _, _ = _call(port, "POST", f"/v0/sandboxes/{sid}/run_code", body={"code": "print(1)"})
    assert status == 401


def test_envd_plane_wrong_token_401(live_server):
    port, _ = live_server
    sid, _ = _create_sandbox(port)
    status, _, _ = _call(
        port,
        "POST",
        f"/v0/sandboxes/{sid}/run_code",
        headers={wire.HEADER_ACCESS_TOKEN: "nope"},
        body={"code": "print(1)"},
    )
    assert status == 401


def test_envd_plane_correct_token_200(live_server):
    port, _ = live_server
    sid, access = _create_sandbox(port)
    status, body, _ = _call(
        port,
        "POST",
        f"/v0/sandboxes/{sid}/run_code",
        headers={wire.HEADER_ACCESS_TOKEN: access},
        body={"code": "print(1)"},
    )
    assert status == 200
    assert json.loads(body)["text"] == "print(1)"


# --- プレーン分離: 認証を相互に流用できない --------------------------


def test_control_key_cannot_authorize_envd(live_server):
    port, _ = live_server
    sid, _ = _create_sandbox(port)
    # 制御プレーンの X-API-KEY では envd 経路を通せない。
    status, _, _ = _call(
        port,
        "POST",
        f"/v0/sandboxes/{sid}/run_code",
        headers={wire.HEADER_API_KEY: API_KEY},
        body={"code": "print(1)"},
    )
    assert status == 401


def test_envd_token_cannot_authorize_control(live_server):
    port, _ = live_server
    sid, access = _create_sandbox(port)
    # envd の X-Access-Token を制御プレーンの鍵として使っても通らない。
    status, _, _ = _call(
        port,
        "GET",
        f"/v0/sandboxes/{sid}",
        headers={wire.HEADER_API_KEY: access},
    )
    assert status == 401
    # X-Access-Token ヘッダで制御プレーンを叩いても（制御は X-API-KEY を見る）401。
    status2, _, _ = _call(
        port,
        "GET",
        f"/v0/sandboxes/{sid}",
        headers={wire.HEADER_ACCESS_TOKEN: access},
    )
    assert status2 == 401
