"""server の認証・default-deny enforce・E2B ワイヤ形応答・ドライバ結線。"""

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
DATA_PORT = 8443  # テスト用のダミー TLS ポート（domain 埋め込みの検証に使う）


def _app(*, isolation=IsolationLevel.VM_PER_CONTAINER, allow_shared_kernel=False) -> ShimApp:
    app = ShimApp(
        driver=MockDriver(isolation_level=isolation),
        api_key=API_KEY,
        allow_shared_kernel=allow_shared_kernel,
        default_template_id="tmpl@sha256:x",
    )
    app.data_port = DATA_PORT
    return app


def _req(method, path, *, headers=None, query=None, body=b"") -> Request:
    return Request(method=method, path=path, headers=headers or {}, query=query or {}, body=body)


def _create(app, *, headers=None, body=None):
    b = json.dumps(body or {}).encode() if body is not None else b""
    return app.dispatch_control(
        _req("POST", "/sandboxes", headers=headers or {wire.HEADER_API_KEY: API_KEY}, body=b)
    )


def _data_headers(sid: str, subdomain_port: int, access: str | None) -> dict[str, str]:
    """データプレーン要求のヘッダ（Host ルーティング + X-Access-Token）を組む。"""
    h = {"Host": f"{subdomain_port}-{sid}.sbx.localhost:{DATA_PORT}"}
    if access is not None:
        h[wire.HEADER_ACCESS_TOKEN] = access
    return h


def _multipart(path: str, data: bytes) -> tuple[str, bytes]:
    """SDK が送る形の multipart/form-data（part 名 file、filename=パス）を組む。"""
    boundary = "testboundary123"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return f"multipart/form-data; boundary={boundary}", body


def _stream_events(resp) -> list[dict]:
    """ストリーム応答の JSON lines をイベント列へ（1 行 = 1 イベント・空行禁止の検証込み）。"""
    assert resp.stream is not None
    lines = b"".join(resp.stream).split(b"\n")
    assert lines[-1] == b""  # 各イベントは改行終端
    assert all(line for line in lines[:-1])  # 空行イベントは SDK クラッシュ（禁止）
    return [json.loads(line) for line in lines[:-1]]


# --- トークン検証（なし／不一致は両経路で 401） -----------------------


def test_control_plane_requires_api_key():
    app = _app()
    # ヘッダなし → 401。
    assert app.dispatch_control(_req("POST", "/sandboxes")).status == 401
    # 不一致 → 401。
    assert _create(app, headers={wire.HEADER_API_KEY: "e2b_" + "9" * 32}).status == 401
    # 一致 → 201。
    assert _create(app).status == 201


def test_envd_plane_requires_access_token():
    app = _app()
    created = json.loads(_create(app).body)
    sid = created["sandboxID"]
    access = created["envdAccessToken"]
    body = json.dumps({"code": "print(1)"}).encode()
    # トークンなし → 401。
    resp = app.dispatch_data(
        _req("POST", "/execute", headers=_data_headers(sid, wire.RUN_CODE_PORT, None), body=body)
    )
    assert resp.status == 401
    assert json.loads(resp.body)["code"] == 401  # E2B エラー形 {"code","message"}
    # 不一致 → 401。
    bad = app.dispatch_data(
        _req("POST", "/execute", headers=_data_headers(sid, wire.RUN_CODE_PORT, "nope"), body=body)
    )
    assert bad.status == 401
    # 制御プレーンの X-API-KEY では envd を通せない（プレーン分離）。
    h = _data_headers(sid, wire.RUN_CODE_PORT, None)
    h[wire.HEADER_API_KEY] = API_KEY
    assert app.dispatch_data(_req("POST", "/execute", headers=h, body=body)).status == 401
    # 別サンドボックスの Host にはトークンが対応しない（sandbox_id との対応検証）。
    other = app.dispatch_data(
        _req(
            "POST",
            "/execute",
            headers=_data_headers("other-sbx", wire.RUN_CODE_PORT, access),
            body=body,
        )
    )
    assert other.status == 401
    # 正しい envd トークン → 200 ストリーム。
    ok = app.dispatch_data(
        _req("POST", "/execute", headers=_data_headers(sid, wire.RUN_CODE_PORT, access), body=body)
    )
    assert ok.status == 200
    events = _stream_events(ok)
    # MockDriver はコードをそのまま主結果として返す。
    main = [e for e in events if e["type"] == "result" and e["is_main_result"]]
    assert main and main[0]["text"] == "print(1)"
    # stdout イベントは timestamp(ns) 必須。
    stdout = [e for e in events if e["type"] == "stdout"]
    assert stdout and isinstance(stdout[0]["timestamp"], int)


# --- create 応答（E2B Sandbox モデル） ---------------------------------------


def test_create_returns_e2b_sandbox_model():
    app = _app()
    resp = _create(app, body={"templateID": "tmpl-x", "metadata": {"k": "v"}, "timeout": 60})
    assert resp.status == 201
    body = json.loads(resp.body)
    # 必須キー: clientID・envdVersion・sandboxID・templateID（欠落は SDK クラッシュ）。
    assert body["templateID"] == "tmpl-x"
    assert body["clientID"]
    assert body["envdVersion"] == wire.ENVD_VERSION
    assert body["sandboxID"]
    assert body["envdAccessToken"]
    # domain は必ず返す（ポート埋め込み——未返却は e2b.app へフォールバックしてしまう）。
    assert body["domain"] == f"sbx.localhost:{DATA_PORT}"


def test_create_without_data_port_is_rejected():
    # データプレーン未 bind では run_code/files が成立しないため 500 で早期検出する。
    app = _app()
    app.data_port = None
    assert _create(app).status == 500


# --- default-deny enforce -------------------------------------


def test_shared_kernel_denied_without_opt_in():
    app = _app(isolation=IsolationLevel.SHARED_KERNEL, allow_shared_kernel=False)
    resp = _create(app)
    assert resp.status == 403
    payload = json.loads(resp.body)
    assert payload["code"] == 403
    assert "denied" in payload["message"]
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
        body={"templateID": "t", "trust": 2, "metadata": {"trust": "2"}},
    )
    assert resp.status == 403


# --- get_info（SandboxDetail 必須 10 キー + metadata round-trip） -------------


def test_get_info_returns_sandbox_detail():
    app = _app(isolation=IsolationLevel.VM_PER_CONTAINER)
    sid = json.loads(_create(app, body={"metadata": {"purpose": "test"}}).body)["sandboxID"]
    resp = app.dispatch_control(
        _req("GET", f"/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    # 必須 10 キー（欠落は SDK の KeyError クラッシュ——spike §1.1）。
    for key in (
        "clientID",
        "cpuCount",
        "diskSizeMB",
        "endAt",
        "envdVersion",
        "memoryMB",
        "sandboxID",
        "startedAt",
        "state",
        "templateID",
    ):
        assert key in body, f"missing required key: {key}"
    assert body["state"] == "running"
    assert body["startedAt"].endswith("Z")
    # metadata round-trip（isolation_level の正典経路 + create 時 metadata の保存）。
    assert body["metadata"]["isolation_level"] == "vm-per-container"
    assert body["metadata"]["purpose"] == "test"
    assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "vm-per-container"


def test_get_info_unknown_sandbox_404():
    app = _app()
    resp = app.dispatch_control(
        _req("GET", "/sandboxes/nope", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert resp.status == 404
    assert json.loads(resp.body)["code"] == 404


# --- files 往復（envd 面・multipart） ----------------------------------------


def test_file_write_read_roundtrip():
    app = _app()
    created = json.loads(_create(app).body)
    sid, access = created["sandboxID"], created["envdAccessToken"]
    h = _data_headers(sid, wire.ENVD_PORT, access)
    ct, mp_body = _multipart("/w/a", b"data")
    h_write = dict(h, **{"Content-Type": ct})
    w = app.dispatch_data(
        _req("POST", "/files", headers=h_write, query={"path": "/w/a"}, body=mp_body)
    )
    assert w.status == 200
    # 非空 JSON 配列応答（空/非配列は SDK の SandboxException——spike §1.2）。
    entries = json.loads(w.body)
    assert isinstance(entries, list) and entries
    assert entries[0] == {"name": "a", "type": "file", "path": "/w/a"}
    r = app.dispatch_data(_req("GET", "/files", headers=h, query={"path": "/w/a"}))
    assert r.status == 200
    assert r.body == b"data"


def test_file_write_uses_part_filename_without_query():
    # path クエリは 1 件時のみ付く。複数件は filename がパスになる（spike §1.2）。
    app = _app()
    created = json.loads(_create(app).body)
    sid, access = created["sandboxID"], created["envdAccessToken"]
    ct, mp_body = _multipart("/from/filename", b"x")
    h = dict(_data_headers(sid, wire.ENVD_PORT, access), **{"Content-Type": ct})
    w = app.dispatch_data(_req("POST", "/files", headers=h, body=mp_body))
    assert w.status == 200
    assert json.loads(w.body)[0]["path"] == "/from/filename"


def test_file_read_missing_404():
    app = _app()
    created = json.loads(_create(app).body)
    sid, access = created["sandboxID"], created["envdAccessToken"]
    h = _data_headers(sid, wire.ENVD_PORT, access)
    resp = app.dispatch_data(_req("GET", "/files", headers=h, query={"path": "/none"}))
    assert resp.status == 404


def test_health_endpoint():
    app = _app()
    created = json.loads(_create(app).body)
    sid, access = created["sandboxID"], created["envdAccessToken"]
    resp = app.dispatch_data(
        _req("GET", "/health", headers=_data_headers(sid, wire.ENVD_PORT, access))
    )
    assert resp.status == 200


# --- destroy（204/404） ------------------------------------------------------


def test_destroy_removes_sandbox_and_token():
    app = _app()
    sid = json.loads(_create(app).body)["sandboxID"]
    assert sid in app.active_sandboxes()
    resp = app.dispatch_control(
        _req("DELETE", f"/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert resp.status == 204  # kill()==True
    assert resp.body == b""
    assert sid not in app.active_sandboxes()
    # 再 destroy は 404（kill()==False。例外なし）。
    again = app.dispatch_control(
        _req("DELETE", f"/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert again.status == 404
    # destroy 済みへの get_info も 404。
    info = app.dispatch_control(
        _req("GET", f"/sandboxes/{sid}", headers={wire.HEADER_API_KEY: API_KEY})
    )
    assert info.status == 404
    # destroy 済みへの envd 要求はトークン失効により 401。
    envd = app.dispatch_data(
        _req("GET", "/health", headers=_data_headers(sid, wire.ENVD_PORT, "stale"))
    )
    assert envd.status == 401


def test_unknown_route_404():
    app = _app()
    resp = app.dispatch_control(_req("GET", "/nope", headers={wire.HEADER_API_KEY: API_KEY}))
    assert resp.status == 404
    assert json.loads(resp.body)["code"] == 404
    # データプレーンの未知 Host も 404。
    bad_host = app.dispatch_data(_req("GET", "/health", headers={"Host": "example.com"}))
    assert bad_host.status == 404


# --- 127.0.0.1 限定 bind ----------------------------------------------------


def test_make_server_rejects_non_loopback():
    with pytest.raises(ValueError):
        make_server(_app(), host="0.0.0.0")


def test_real_socket_roundtrip():
    # 実ソケットで制御プレーン 1 往復（127.0.0.1 bind・stdlib のみ）。
    app = _app(isolation=IsolationLevel.VM_PER_CONTAINER)
    server = make_server(app, host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/sandboxes",
            data=json.dumps({"templateID": "t"}).encode(),
            headers={wire.HEADER_API_KEY: API_KEY, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
            assert resp.headers[wire.HEADER_ISOLATION_LEVEL] == "vm-per-container"
            data = json.loads(resp.read())
            assert data["envdAccessToken"]
            assert data["domain"] == f"sbx.localhost:{DATA_PORT}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)
