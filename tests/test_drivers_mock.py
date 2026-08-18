"""MockDriver の契約テスト。CI のユニット／契約テストの主役。"""

from __future__ import annotations

import pytest

from subaco_shim.drivers import _commands as C
from subaco_shim.drivers.mock import (
    MockDriver,
    MockFileNotFoundError,
    MockSandboxNotFoundError,
)
from subaco_shim.isolation import IsolationLevel


def test_available_always_true():
    assert MockDriver.available() is True


def test_create_records_per_sandbox_network_and_isolation():
    d = MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER)
    info = d.create(template_id="tmpl@sha256:abc")
    # 隔離レベルは metadata に載る（正典な返却経路）。
    assert info.isolation_level is IsolationLevel.VM_PER_CONTAINER
    # サンドボックス個別ネットワークが作成されている。
    net = C.network_name(info.sandbox_id)
    assert net in d.created_networks
    assert net in d.live_networks
    # コマンド列に internal ネットワーク作成が記録されている（--network=none は使わない）。
    assert [C.PODMAN, "network", "create", "--internal", "--disable-dns", net] in d.commands


def test_full_roundtrip_and_network_cleanup():
    d = MockDriver()
    info = d.create(template_id="tmpl")
    sid = info.sandbox_id
    # exec は Execution を返す（.text は主結果）。
    assert d.exec(sid, "print(42)").text == "print(42)"
    # put/get ファイル往復。
    d.put_file(sid, "/work/a.txt", b"hello")
    assert d.get_file(sid, "/work/a.txt") == b"hello"
    # get_info は隔離レベル込みで返る。
    assert d.get_info(sid).isolation_level is IsolationLevel.SHARED_KERNEL
    # destroy でネットワーク残骸が掃除される。
    net = C.network_name(sid)
    d.destroy(sid)
    assert net in d.removed_networks
    assert net not in d.live_networks
    assert [C.PODMAN, "network", "rm", net] in d.commands


def test_operations_on_unknown_sandbox_raise():
    d = MockDriver()
    with pytest.raises(MockSandboxNotFoundError):
        d.exec("nope", "x")
    with pytest.raises(MockSandboxNotFoundError):
        d.get_info("nope")


def test_missing_file_raises():
    d = MockDriver()
    info = d.create(template_id="tmpl")
    with pytest.raises(MockFileNotFoundError):
        d.get_file(info.sandbox_id, "/absent")


def test_no_host_mount_flag_in_run_command():
    # ホストマウント禁止: run コマンドに -v が現れないこと。
    d = MockDriver()
    d.create(template_id="tmpl")
    run_cmds = [c for c in d.commands if len(c) > 1 and c[1] == "run"]
    assert run_cmds
    for cmd in run_cmds:
        assert "-v" not in cmd
