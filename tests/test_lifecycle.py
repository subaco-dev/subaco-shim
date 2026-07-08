"""lifecycle の単一インスタンス・ポート公開・トークン再利用・アイドル終了。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from subaco_shim.config import CubePaths, ShimConfig
from subaco_shim.drivers.mock import MockDriver
from subaco_shim.isolation import IsolationLevel
from subaco_shim.lifecycle import start_shim
from subaco_shim.protocol import wire
from subaco_shim.tokens import SingleInstanceError, read_port, read_token

API_KEY_HEADER = wire.HEADER_API_KEY


def _config() -> ShimConfig:
    return ShimConfig()  # allow_shared_kernel=False（既定）。


def _vm_driver() -> MockDriver:
    return MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER)


def test_port_published_and_request_roundtrip(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    shim = start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0)
    # .cube/port に実ポートが公開されている。
    assert read_port(paths) == shim.port
    token = read_token(paths)
    t = threading.Thread(target=shim.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{shim.port}/v0/sandboxes",
            data=json.dumps({"template_id": "t"}).encode(),
            headers={API_KEY_HEADER: token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
    finally:
        shim.shutdown()
        t.join(timeout=5)
        shim.close()


def test_token_reused_across_restarts(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    shim1 = start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0)
    first_token = read_token(paths)
    first_port = shim1.port
    shim1.close()  # アイドル終了相当。
    # 再起動: 永続トークンを再利用。ポートは変わり得るが .cube/port で解決可能。
    shim2 = start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0)
    try:
        assert read_token(paths) == first_token
        assert read_port(paths) == shim2.port
        # 起動ヘルパーはトークンを再読込するため、旧ポートが変わっても認証は維持される。
        assert first_token is not None
        _ = first_port
    finally:
        shim2.close()


def test_single_instance_enforced(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    shim1 = start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0)
    try:
        with pytest.raises(SingleInstanceError):
            start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0)
    finally:
        shim1.close()
    # 解放後は再取得できる。
    start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0).close()


def test_idle_timeout_terminates(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    shim = start_shim(paths=paths, config=_config(), driver=_vm_driver(), idle_timeout=0.3)
    t = threading.Thread(target=shim.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    # 無操作なのでアイドル閾値超過で自動終了する。
    t.join(timeout=5)
    assert not t.is_alive()
    shim.close()


def test_destroy_cleans_networks_on_shutdown(tmp_path):
    # シャットダウン時に生存サンドボックスを destroy し、個別ネットワークを掃除する。
    paths = CubePaths.resolve(tmp_path)
    driver = _vm_driver()
    shim = start_shim(paths=paths, config=_config(), driver=driver, idle_timeout=0)
    info = driver.create(template_id="t")
    shim.app._envd_tokens[info.sandbox_id] = "tok"  # create 経由相当に登録。
    shim.close()
    # destroy によりネットワークが掃除されている。
    from subaco_shim.drivers import _commands as C

    assert C.network_name(info.sandbox_id) in driver.removed_networks
