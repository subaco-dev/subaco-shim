"""wslc スケルトンドライバの import・ガードとドライバレジストリ。

apple_container は M2b-1 で本実装になった（tests/test_drivers_apple_container.py）。
ここに残るのは wslc スケルトンのガードと、レジストリ／auto 選択の検証。
"""

from __future__ import annotations

import pytest

from subaco_shim.drivers import build_driver, select_driver
from subaco_shim.drivers.apple_container import AppleContainerDriver
from subaco_shim.drivers.mock import MockDriver
from subaco_shim.drivers.wslc import WslcDriver
from subaco_shim.isolation import IsolationLevel


def test_skeleton_isolation_levels():
    # Apple Container = vm-per-container、wslc = shared-kernel。
    assert AppleContainerDriver.isolation_level is IsolationLevel.VM_PER_CONTAINER
    assert WslcDriver.isolation_level is IsolationLevel.SHARED_KERNEL
    assert WslcDriver.experimental is True


def test_wslc_native_windows_guarded(monkeypatch):
    monkeypatch.setattr("subaco_shim.drivers.wslc.platform.system", lambda: "Windows")
    d = WslcDriver()
    with pytest.raises(NotImplementedError):
        d.create(template_id="tmpl")


def test_build_driver_registry():
    assert isinstance(build_driver("mock"), MockDriver)
    assert isinstance(build_driver("container"), AppleContainerDriver)
    assert isinstance(build_driver("wslc"), WslcDriver)
    with pytest.raises(ValueError):
        build_driver("bogus")


def test_build_driver_auto_uses_select(monkeypatch):
    # auto は select_driver に委譲。全バックエンド不在なら RuntimeError。
    monkeypatch.setattr(
        "subaco_shim.drivers.AppleContainerDriver.available", classmethod(lambda cls: False)
    )
    monkeypatch.setattr(
        "subaco_shim.drivers.PodmanDriver.available", classmethod(lambda cls: False)
    )
    monkeypatch.setattr("subaco_shim.drivers.WslcDriver.available", classmethod(lambda cls: False))
    with pytest.raises(RuntimeError):
        select_driver()
