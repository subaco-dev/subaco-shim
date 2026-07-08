"""server の認証・default-deny enforce・ドライバ結線。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from subaco_shim.drivers.mock import MockDriver
from subaco_shim.isolation import IsolationLevel
from subaco_shim.protocol import wire
from subaco_shim.server import Request, ShimApp, make_server

API_KEY = "e2b_" + "0" * 32


def _app(*, isolation=IsolationLevel.VM_PER_CONTAINER, allow_shared_kernel=False) -> ShimApp:
    return ShimApp(
        driver=MockDriver(isolation_level=isolation),
        api_key=API_KEY,
        allow_shared_kernel=allow_shared_kernel,
        default_template_id="tmpl@sha256:x",
    )


def _req(method, path, *, headers=None, query=None, body=b"") -> Request:
    return Request(method=method, path=path, headers=headers or {}, query=query or {}, body=body)


def _create(app, *, headers=None, body=None):
    b = json.dumps(body or {}).encode() if body is not None else b""
    return app.dispatch(
        _req("POST", "/v0/sandboxes", headers=headers or {wire.HEADER_API_KEY: API_KEY}, body=b)
    )


# --- トークン検証（なし／不一致は両経路で 401） -----------------------


def test_control_plane_requires_api_key():
    app = _app()
    # ヘッダなし → 401。
    assert app.dispatch(_req("POST", "/v0/sandboxes")).status == 401
    # 不一致 → 401。
    assert _create(app, headers={wire.HEADER_API_KEY: "e2b_" + "9" * 32}).status == 401
    # 一致 → 201。
    assert _create(app).status == 201


def test_envd_plane_requires_access_token():
    app = _app()
    created = json.loads(_create(app).body)
    sid = created["sandbox_id"]
    access = created["envd_access_token"]
    run_path = f"/v0/sandboxes/{sid}/run_code"
    body = json.dumps({"code": "print(1)"}).encode()
    # トークンなし → 401。
    assert app.dispatch(_req("POST", run_path, body=body)).status == 401
    # 不一致 → 401。
    bad = app.dispatch(
        _req("POST", run_path, headers={wire.HEADER_ACCESS_TOKEN: "nope"}, body=body)
    )
    assert bad.status == 401
    # 制御プレーンの X-API-KEY では envd を通せない（プレーン分離）。
    wrong = app.dispatch(_req("POST", run_path, headers={wire.HEADER_API_KEY: API_KEY}, body=body))
    assert wrong.status == 401
    # 正しい envd トークン → 200。
    ok = app.dispatch(_req("POST", run_path, headers={wire.HEADER_ACCESS_TOKEN: access}, body=body))
    assert ok.status == 200
    assert json.loads(ok.body)["text"] == "print(1)"


# --- default-deny enforce -------------------------------------


def test_shared_kernel_denied_without_opt_in():
    app = _app(isolation=IsolationLevel.SHARED_KERNEL, allow_shared_kernel=False)
    resp = _create(app)
    assert resp.status == 403
    payload = json.loads(resp.body)
    assert payload["reason"] == "denied:shared-kernel-not-opted-in"
    assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "shared-kernel"


def test_shared_kernel_allowed_with_opt_in():
    app = _app(isolation=IsolationLevel.SHARED_KERNEL, allow_shared_kernel=True)
    assert _create(app).status == 201


def test_vm_per_container_allowed_without_opt_in():
    app = _app(isolation=IsolationLevel.VM_PER_CONTAINER, allow_shared_kernel=False)
    resp = _create(app)
    assert resp.status == 201
    assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "vm-per-container"


def test_agent_trust_claim_does_not_relax_routing():
    # エージェント申告の trust（本文/ヘッダ）で shared-kernel 拒否を緩和しないこと。
    app = _app(isolation=IsolationLevel.SHARED_KERNEL, allow_shared_kernel=False)
    resp = _create(
        app,
        headers={wire.HEADER_API_KEY: API_KEY, "X-Agent-Trust": "2"},
        body={"template_id": "t", "trust": 2, "metadata": {"trust": "2"}},
    )
    assert resp.status == 403


# --- get_info の隔離レベル返却（metadata + X-Isolation-Level） ----------------


def test_get_info_returns_isolation_level():
    app = _app(isolation=IsolationLevel.VM_PER_CONTAINER)
    sid = json.loads(_create(app).body)["sandbox_id"]
    resp = app.dispatch(_req("GET", f"/v0/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["metadata"]["isolation_level"] == "vm-per-container"
    assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "vm-per-container"


# --- files 往復（envd） ------------------------------------------------------


def test_file_write_read_roundtrip():
    app = _app()
    created = json.loads(_create(app).body)
    sid, access = created["sandbox_id"], created["envd_access_token"]
    h = {wire.HEADER_ACCESS_TOKEN: access}
    w = app.dispatch(
        _req("POST", f"/v0/sandboxes/{sid}/files", headers=h, query={"path": "/w/a"}, body=b"data")
    )
    assert w.status == 200
    r = app.dispatch(_req("GET", f"/v0/sandboxes/{sid}/files", headers=h, query={"path": "/w/a"}))
    assert r.status == 200
    assert r.body == b"data"


def test_destroy_removes_sandbox_and_token():
    app = _app()
    sid = json.loads(_create(app).body)["sandbox_id"]
    assert sid in app.active_sandboxes()
    resp = app.dispatch(
        _req("DELETE", f"/v0/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert resp.status == 200
    assert sid not in app.active_sandboxes()


def test_unknown_route_404():
    app = _app()
    assert app.dispatch(_req("GET", "/nope")).status == 404


# --- 127.0.0.1 限定 bind ----------------------------------------------------


def test_make_server_rejects_non_loopback():
    with pytest.raises(ValueError):
        make_server(_app(), host="0.0.0.0")


def test_real_socket_roundtrip():
    # 実ソケットで 1 往復（127.0.0.1 bind・stdlib のみ）。
    app = _app(isolation=IsolationLevel.VM_PER_CONTAINER)
    server = make_server(app, host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v0/sandboxes",
            data=json.dumps({"template_id": "t"}).encode(),
            headers={wire.HEADER_API_KEY: API_KEY, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
            assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "vm-per-container"
            data = json.loads(resp.read())
            assert "envd_access_token" in data
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)
