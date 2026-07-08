"""podman ドライバの import 安全性・argv ビルダ・rootless 前提チェック。

このマシンには podman が無い前提。実コンテナ経路は Linux CI（ubuntu）で検証する。
ここでは「podman 不在でも import 可能」「argv 生成」「前提チェックのパース」を検証する。
"""

from __future__ import annotations

import shutil

import pytest

from subaco_shim.drivers import _commands as C
from subaco_shim.drivers.podman import (
    RUNBOOK_ROOTLESS,
    PodmanDriver,
    PodmanPreflightError,
    PodmanUnavailableError,
    check_rootless_prerequisites,
)
from subaco_shim.isolation import IsolationLevel


def test_import_and_isolation_level():
    # 遅延検出のためインスタンス化は podman 無しでも可能。
    d = PodmanDriver()
    assert d.isolation_level is IsolationLevel.SHARED_KERNEL
    assert d.name == "podman"


def test_available_reflects_binary_presence():
    # 実バイナリの有無に一致する（このマシンでは通常 False）。
    assert PodmanDriver.available() == (shutil.which("podman") is not None)


def test_create_raises_when_binary_absent(monkeypatch):
    # 検出を強制的に「不在」にして、create が明示エラーを送出することを確認。
    monkeypatch.setattr("subaco_shim.drivers.podman._detect_binary", lambda: None)
    d = PodmanDriver()
    with pytest.raises(PodmanUnavailableError):
        d.create(template_id="tmpl")


def test_preflight_error_carries_runbook(monkeypatch):
    # バイナリはあるが rootless 前提が欠ける状況を模擬。
    monkeypatch.setattr("subaco_shim.drivers.podman._detect_binary", lambda: "/usr/bin/podman")
    monkeypatch.setattr(
        "subaco_shim.drivers.podman.check_rootless_prerequisites",
        lambda: ["missing-subuid"],
    )
    d = PodmanDriver()
    with pytest.raises(PodmanPreflightError) as exc:
        d.create(template_id="tmpl")
    # runbook 断片が付随する（受け入れ条件）。
    assert "subuid" in str(exc.value)
    assert RUNBOOK_ROOTLESS


def test_preflight_skips_on_non_linux(monkeypatch):
    # Linux 以外（このマシン=Darwin 想定）では前提チェックは空（対象外）。
    monkeypatch.setattr("subaco_shim.drivers.podman.platform.system", lambda: "Darwin")
    assert check_rootless_prerequisites() == []


def test_preflight_detects_missing_on_linux(monkeypatch):
    # Linux・非 NixOS で subuid/subgid/newuidmap がすべて欠ける状況を模擬。
    monkeypatch.setattr("subaco_shim.drivers.podman.platform.system", lambda: "Linux")
    monkeypatch.setattr("subaco_shim.drivers.podman._is_nixos", lambda: False)
    monkeypatch.setattr("subaco_shim.drivers.podman._subid_registered", lambda *a, **k: False)
    monkeypatch.setattr("subaco_shim.drivers.podman._has_setuid", lambda name: False)
    problems = check_rootless_prerequisites()
    assert set(problems) == {
        "missing-subuid",
        "missing-subgid",
        "missing-setuid-newuidmap",
        "missing-setuid-newgidmap",
    }


def test_argv_builders():
    # 個別ネットワーク作成/掃除・exec・put/get の argv 形（--network=none は使わない）。
    net = C.network_name("abc123")
    assert C.create_network_argv(net) == ["network", "create", "--internal", "cube-abc123"]
    assert C.remove_network_argv(net) == ["network", "rm", "cube-abc123"]
    run = C.run_container_argv(C.container_name("abc123"), net, "img")
    assert "-v" not in run  # ホストマウント禁止。
    assert "--network" in run and net in run
    assert C.get_file_argv("cube-sb-abc123", "/p") == ["exec", "cube-sb-abc123", "cat", "--", "/p"]
