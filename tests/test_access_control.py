"""両プレーンのアクセス制御を実ソケットで検証する。

server を 127.0.0.1 の空きポートで実起動し、``http.client`` で HTTP 往復して次を確認する:

- **制御プレーン**（create / get_info / destroy）は ``X-API-KEY`` を要求し、なし／不一致は 401。
- **データプレーン**（run_code / files。Host ヘッダ ``{port}-{sandbox_id}.sbx.localhost`` で
  ルーティング）はサンドボックス単位の ``X-Access-Token`` を要求し、なし／不一致は 401。
- **プレーン分離**: 制御プレーンの鍵で envd は通せず、envd トークンで制御プレーンは通せない
  （相互に認証を流用できない）。

データプレーンリスナーは認証境界の検証に TLS を要しないため平文で bind する
（TLS 込みの実 SDK 経路は tests/test_wire_contract.py が検証する）。
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
    """127.0.0.1 の空きポートで両プレーンを実起動し (ctrl_port, data_port, app) を返す。"""
    app = ShimApp(
        driver=MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER),
        api_key=API_KEY,
        allow_shared_kernel=False,
        default_template_id="tmpl@sha256:x",
    )
    ctrl = make_server(app, host="127.0.0.1", port=0, plane="control")
    data = make_server(app, host="127.0.0.1", port=0, plane="data")
    app.data_port = data.server_address[1]
    threads = [
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        for s in (ctrl, data)
    ]
    for t in threads:
        t.start()
    try:
        yield ctrl.server_address[1], data.server_address[1], app
    finally:
        for s in (ctrl, data):
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join(timeout=5)


def _call(port, method, path, *, headers=None, body=None, raw_body=None):
    """http.client で 1 往復し (status, body_bytes, headers_dict) を返す。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = (
            raw_body
            if raw_body is not None
            else (json.dumps(body).encode() if body is not None else None)
        )
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = dict(resp.getheaders())
        return resp.status, payload, hdrs
    finally:
        conn.close()


def _data_headers(sid, subdomain_port, access, extra=None):
    """データプレーン要求のヘッダ（Host ルーティング + 任意の X-Access-Token）。"""
    h = {"Host": f"{subdomain_port}-{sid}.sbx.localhost"}
    if access is not None:
        h[wire.HEADER_ACCESS_TOKEN] = access
    h.update(extra or {})
    return h


def _create_sandbox(port):
    """正しい制御鍵でサンドボックスを 1 つ作り (sandbox_id, envd_access_token) を返す。"""
    status, body, _ = _call(
        port,
        "POST",
        "/sandboxes",
        headers={wire.HEADER_API_KEY: API_KEY},
        body={"templateID": "tmpl"},
    )
    assert status == 201, body
    data = json.loads(body)
    return data["sandboxID"], data["envdAccessToken"]


# --- 制御プレーン: X-API-KEY ---------------------------------


def test_control_plane_missing_key_401(live_server):
    ctrl, _, _ = live_server
    status, body, _ = _call(ctrl, "POST", "/sandboxes", body={"templateID": "t"})
    assert status == 401
    assert json.loads(body) == {"code": 401, "message": "missing or invalid API key"}


def test_control_plane_wrong_key_401(live_server):
    ctrl, _, _ = live_server
    status, _, _ = _call(
        ctrl,
        "POST",
        "/sandboxes",
        headers={wire.HEADER_API_KEY: "e2b_" + "9" * 32},
        body={"templateID": "t"},
    )
    assert status == 401


def test_control_plane_correct_key_201(live_server):
    ctrl, data_port, _ = live_server
    status, body, _ = _call(
        ctrl,
        "POST",
        "/sandboxes",
        headers={wire.HEADER_API_KEY: API_KEY},
        body={"templateID": "t"},
    )
    assert status == 201
    data = json.loads(body)
    assert data["sandboxID"]
    assert data["envdAccessToken"]
    assert data["domain"] == f"sbx.localhost:{data_port}"


def test_control_info_and_destroy_require_key(live_server):
    ctrl, _, _ = live_server
    sid, _ = _create_sandbox(ctrl)
    # get_info / destroy も制御プレーン。鍵なしは 401。
    assert _call(ctrl, "GET", f"/sandboxes/{sid}")[0] == 401
    assert _call(ctrl, "DELETE", f"/sandboxes/{sid}")[0] == 401
    # 正しい鍵なら 200。
    ok = _call(ctrl, "GET", f"/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    assert ok[0] == 200


# --- データプレーン: X-Access-Token（サンドボックス単位） ----------------------


def test_envd_plane_missing_token_401(live_server):
    ctrl, data_port, _ = live_server
    sid, _ = _create_sandbox(ctrl)
    status, body, _ = _call(
        data_port,
        "POST",
        "/execute",
        headers=_data_headers(sid, wire.RUN_CODE_PORT, None),
        body={"code": "print(1)"},
    )
    assert status == 401
    assert json.loads(body)["code"] == 401


def test_envd_plane_wrong_token_401(live_server):
    ctrl, data_port, _ = live_server
    sid, _ = _create_sandbox(ctrl)
    status, _, _ = _call(
        data_port,
        "POST",
        "/execute",
        headers=_data_headers(sid, wire.RUN_CODE_PORT, "nope"),
        body={"code": "print(1)"},
    )
    assert status == 401


def test_envd_plane_correct_token_200(live_server):
    ctrl, data_port, _ = live_server
    sid, access = _create_sandbox(ctrl)
    status, body, _ = _call(
        data_port,
        "POST",
        "/execute",
        headers=_data_headers(sid, wire.RUN_CODE_PORT, access),
        body={"code": "print(1)"},
    )
    assert status == 200
    # chunked JSON lines（http.client がデチャンクする）。主結果 = コードそのもの（Mock）。
    events = [json.loads(line) for line in body.splitlines() if line]
    main = [e for e in events if e["type"] == "result" and e["is_main_result"]]
    assert main and main[0]["text"] == "print(1)"


def test_envd_token_not_valid_for_other_sandbox(live_server):
    ctrl, data_port, _ = live_server
    _, access_a = _create_sandbox(ctrl)
    sid_b, _ = _create_sandbox(ctrl)
    # サンドボックス A のトークンでは B の Host 経路を通せない（多重化キーの分離）。
    status, _, _ = _call(
        data_port,
        "GET",
        "/health",
        headers=_data_headers(sid_b, wire.ENVD_PORT, access_a),
    )
    assert status == 401


# --- プレーン分離: 認証を相互に流用できない --------------------------


def test_control_key_cannot_authorize_envd(live_server):
    ctrl, data_port, _ = live_server
    sid, _ = _create_sandbox(ctrl)
    # 制御プレーンの X-API-KEY では envd 経路を通せない。
    status, _, _ = _call(
        data_port,
        "POST",
        "/execute",
        headers=_data_headers(sid, wire.RUN_CODE_PORT, None, {wire.HEADER_API_KEY: API_KEY}),
        body={"code": "print(1)"},
    )
    assert status == 401


def test_envd_token_cannot_authorize_control(live_server):
    ctrl, _, _ = live_server
    sid, access = _create_sandbox(ctrl)
    # envd の X-Access-Token を制御プレーンの鍵として使っても通らない。
    status, _, _ = _call(
        ctrl,
        "GET",
        f"/sandboxes/{sid}",
        headers={wire.HEADER_API_KEY: access},
    )
    assert status == 401
    # X-Access-Token ヘッダで制御プレーンを叩いても（制御は X-API-KEY を見る）401。
    status2, _, _ = _call(
        ctrl,
        "GET",
        f"/sandboxes/{sid}",
        headers={wire.HEADER_ACCESS_TOKEN: access},
    )
    assert status2 == 401
