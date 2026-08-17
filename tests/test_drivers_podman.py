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


# --- exec_start（drain スレッド・タイムアウト・キャンセル）: fake バイナリで実プロセス検証 ---


def _fake_podman(tmp_path, script_body: str):
    """podman の代わりに使う実行可能スクリプト（exec サブコマンドを模擬）。"""
    fake = tmp_path / "fake-podman"
    fake.write_text(f"#!/bin/sh\n{script_body}\n")
    fake.chmod(0o755)
    return str(fake)


def test_exec_start_drains_large_output_without_deadlock(tmp_path):
    """pipe 容量超の大量出力でもデッドロックせず、ドライバ側 timeout が実効すること。

    レビュー指摘の再現: 「待ってから読む」方式では 5MB 出力でプロセスが write
    ブロックしたまま終了できず、timeout も効かなかった。drain スレッドが開始直後から
    pipe を読み続けることで、大量出力でも timeout 超過 → kill → ExecTimeout になる。
    """
    import time

    # 5MB 出力（pipe 容量 64KB を大きく超える）後に長時間 sleep = 終わらない実行。
    binary = _fake_podman(tmp_path, "head -c 5000000 /dev/zero | tr '\\0' 'a'\nsleep 30")
    d = PodmanDriver(binary=binary, exec_timeout=1.0)
    start = time.monotonic()
    handle = d.exec_start("sbx1", "code")
    while not handle.done() and time.monotonic() - start < 10:
        time.sleep(0.05)
    assert handle.done(), "大量出力でハンドルがデッドロックしてはならない"
    execution = handle.result()
    elapsed = time.monotonic() - start
    assert elapsed < 8, f"ドライバ timeout(1s) が実効していない: {elapsed:.1f}s"
    assert execution.error is not None
    assert execution.error.name == "ExecTimeout"


def test_exec_start_cancel_kills_running_process(tmp_path):
    import time

    binary = _fake_podman(tmp_path, "sleep 30")
    d = PodmanDriver(binary=binary, exec_timeout=60.0)
    start = time.monotonic()
    handle = d.exec_start("sbx1", "code")
    handle.cancel()
    execution = handle.result()  # kill 済みのため速やかに返る。
    assert time.monotonic() - start < 5
    assert execution.error is not None
    assert execution.error.name == "Cancelled"


def test_exec_start_normal_completion(tmp_path):
    binary = _fake_podman(tmp_path, "echo out-line")
    d = PodmanDriver(binary=binary, exec_timeout=10.0)
    execution = d.exec("sbx1", "code")
    assert execution.error is None
    assert execution.text == "out-line\n"
    assert execution.logs.stdout == ["out-line"]


def test_exec_output_capped_without_oom(tmp_path):
    """未信頼コードの大量出力はホスト側で上限まで蓄積し、超過は読み捨て + 注記する。"""
    # 8MB 出力（上限 1MB の 8 倍）。読み捨てが機能すればデッドロックせず完走する。
    binary = _fake_podman(tmp_path, "head -c 8000000 /dev/zero | tr '\\0' 'a'")
    d = PodmanDriver(binary=binary, exec_timeout=30.0, exec_max_output=1024 * 1024)
    execution = d.exec("sbx1", "code")
    total = sum(len(line) for line in execution.logs.stdout)
    assert total <= 1024 * 1024, "蓄積が上限を超えてはならない"
    # 切り詰めの事実は stderr に注記される（結果欠落の明示）。
    assert any("truncated" in line for line in execution.logs.stderr)


def test_exec_timeout_env_override(tmp_path, monkeypatch):
    """SUBACO_SHIM_EXEC_TIMEOUT でハード上限を設定できる（0 以下 = 無期限）。"""
    import time

    binary = _fake_podman(tmp_path, "sleep 2\necho done")
    # 1 秒制限 → ExecTimeout。
    monkeypatch.setenv("SUBACO_SHIM_EXEC_TIMEOUT", "1")
    d1 = PodmanDriver(binary=binary)
    ex1 = d1.exec("sbx1", "code")
    assert ex1.error is not None and ex1.error.name == "ExecTimeout"
    # 0 = 無期限 → 同じスクリプトが完走する（既定 120 秒固定の打ち切りが無いことの対比）。
    monkeypatch.setenv("SUBACO_SHIM_EXEC_TIMEOUT", "0")
    d2 = PodmanDriver(binary=binary)
    start = time.monotonic()
    ex2 = d2.exec("sbx1", "code")
    assert time.monotonic() - start < 30
    assert ex2.error is None
    assert ex2.text == "done\n"
