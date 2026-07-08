"""drivers/base.py の ABC 契約テスト。"""

from __future__ import annotations

import inspect

import pytest

from subaco_shim.drivers import Driver
from subaco_shim.isolation import IsolationLevel
from subaco_shim.models import Execution, SandboxInfo


def test_driver_is_abstract():
    with pytest.raises(TypeError):
        Driver()  # type: ignore[abstract]


def test_abstract_method_set():
    # 抽象インターフェース + get_info。
    expected = {"create", "exec", "put_file", "get_file", "destroy", "get_info"}
    assert expected <= Driver.__abstractmethods__


def test_fake_driver_roundtrip():
    """3 値のいずれかを宣言する具体ドライバの最小実装が成立すること。"""

    class FakeDriver(Driver):
        name = "fake"
        isolation_level = IsolationLevel.VM_PER_CONTAINER

        def create(self, *, template_id, metadata=None):
            info = SandboxInfo(
                sandbox_id="sb1",
                template_id=template_id,
                metadata=dict(metadata or {}),
            )
            return info.with_isolation_level(self.isolation_level)

        def exec(self, sandbox_id, code):
            from subaco_shim.models import Result

            return Execution(results=[Result(text=code, is_main_result=True)])

        def put_file(self, sandbox_id, path, data):
            return None

        def get_file(self, sandbox_id, path):
            return b""

        def destroy(self, sandbox_id):
            return None

        def get_info(self, sandbox_id):
            info = SandboxInfo(sandbox_id=sandbox_id, template_id="tmpl")
            return info.with_isolation_level(self.isolation_level)

    d = FakeDriver()
    info = d.create(template_id="tmpl")
    # ドライバは 3 値のいずれか（unknown 禁止）を metadata に載せる。
    assert info.isolation_level is IsolationLevel.VM_PER_CONTAINER
    assert d.exec("sb1", "print(1)").text == "print(1)"
    assert d.get_info("sb1").isolation_level is IsolationLevel.VM_PER_CONTAINER


def test_exec_named_per_interface():
    # 抽象インターフェース名 "exec"（run_code 相当）を守る。
    assert "exec" in {name for name, _ in inspect.getmembers(Driver)}


def test_available_default_false():
    assert Driver.available() is False
