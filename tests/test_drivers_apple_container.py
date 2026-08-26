"""Apple Container ドライバの import 安全性・検出・argv ビルダ・前提チェック。

実コンテナ（軽量 VM）経路は Apple Silicon 実機の live スイート
（test_live_apple_container_isolation.py と test_driver_contract の opt-in）で検証する。
ここでは「CLI 不在でも import 可能」「Darwin ガード付き検出」「argv 生成（podman との
CLI 差分）」「前提チェック（macOS 26・サービス稼働）」「共有実行機構への配線」を、
fake バイナリで CI 可搬（ubuntu でも走る）に検証する。
"""

from __future__ import annotations

import subprocess

import pytest

from subaco_shim.drivers import _commands as C
from subaco_shim.drivers import _commands_apple as AC
from subaco_shim.drivers.apple_container import (
    RUNBOOK_APPLE,
    AppleContainerCommandError,
    AppleContainerDriver,
    AppleContainerPreflightError,
    AppleContainerUnavailableError,
)
from subaco_shim.isolation import IsolationLevel

_MOD = "subaco_shim.drivers.apple_container"


@pytest.fixture
def darwin26(monkeypatch):
    """macOS 26 実機相当の platform を模擬する（テストを ubuntu CI でも可搬に）。"""
    monkeypatch.setattr(f"{_MOD}.platform.system", lambda: "Darwin")
    monkeypatch.setattr(f"{_MOD}._macos_major", lambda: 26)


def _fake_container(tmp_path, script_body: str = "exit 0") -> str:
    """container の代わりに使う実行可能スクリプト。

    ``system status``（前提チェック）は既定で成功させる。サブコマンドごとの挙動は
    script_body 側で ``$1`` を見て分岐する（podman テストと同じ技法）。
    """
    fake = tmp_path / "fake-container"
    fake.write_text(f"#!/bin/sh\n{script_body}\n")
    fake.chmod(0o755)
    return str(fake)


def test_import_and_isolation_level():
    # 遅延検出のためインスタンス化は container 無しでも可能。
    d = AppleContainerDriver()
    assert d.isolation_level is IsolationLevel.VM_PER_CONTAINER
    assert d.name == "container"


def test_available_requires_darwin(monkeypatch):
    # `container` は一般語——Linux に同名バイナリがあっても available は False。
    # auto 選択は本ドライバを最優先するため、この Darwin ガードが無いと Linux CI で
    # 無関係のバイナリを掴む（設計上の要点）。
    monkeypatch.setattr(f"{_MOD}.platform.system", lambda: "Linux")
    monkeypatch.setattr(f"{_MOD}._detect_binary", lambda: "/usr/bin/container")
    assert AppleContainerDriver.available() is False


def test_available_on_darwin_reflects_binary(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.platform.system", lambda: "Darwin")
    monkeypatch.setattr(f"{_MOD}._detect_binary", lambda: "/usr/local/bin/container")
    assert AppleContainerDriver.available() is True
    monkeypatch.setattr(f"{_MOD}._detect_binary", lambda: None)
    assert AppleContainerDriver.available() is False


def test_detect_binary_prefers_path_then_system(monkeypatch):
    from subaco_shim.drivers import apple_container as A

    # PATH 優先（podman と同じ規則）。
    monkeypatch.setattr(A.shutil, "which", lambda name: "/custom/bin/container")
    assert A._detect_binary() == "/custom/bin/container"
    # PATH に無ければ installer pkg の既知パスへフォールバック。
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    monkeypatch.setattr(A.os.path, "isfile", lambda p: p == "/usr/local/bin/container")
    monkeypatch.setattr(A.os, "access", lambda p, m: True)
    assert A._detect_binary() == "/usr/local/bin/container"


def test_create_raises_when_binary_absent(monkeypatch, darwin26):
    monkeypatch.setattr(f"{_MOD}._detect_binary", lambda: None)
    d = AppleContainerDriver()
    with pytest.raises(AppleContainerUnavailableError) as exc:
        d.create(template_id="tmpl")
    # runbook（インストール手順）が付随する。
    assert "releases" in str(exc.value)


def test_create_raises_on_non_darwin(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.platform.system", lambda: "Linux")
    d = AppleContainerDriver(binary="/usr/bin/container")
    with pytest.raises(AppleContainerUnavailableError):
        d.create(template_id="tmpl")


def test_preflight_rejects_old_macos(tmp_path, monkeypatch):
    # macOS 15 では複数ネットワーク・コンテナ間分離が成立しない → runbook 付きで弾く。
    monkeypatch.setattr(f"{_MOD}.platform.system", lambda: "Darwin")
    monkeypatch.setattr(f"{_MOD}._macos_major", lambda: 15)
    d = AppleContainerDriver(binary=_fake_container(tmp_path))
    with pytest.raises(AppleContainerPreflightError) as exc:
        d.create(template_id="tmpl")
    assert "macOS 26" in str(exc.value)
    assert RUNBOOK_APPLE.splitlines()[0] in str(exc.value)


def test_preflight_rejects_when_service_not_running(tmp_path, darwin26):
    # `container system status` が非ゼロ → サービス未稼働として runbook 付きで弾く。
    binary = _fake_container(
        tmp_path, 'if [ "$1" = system ]; then echo "not running" >&2; exit 1; fi\nexit 0'
    )
    d = AppleContainerDriver(binary=binary)
    with pytest.raises(AppleContainerPreflightError) as exc:
        d.create(template_id="tmpl")
    assert "container system start" in str(exc.value)


def test_argv_builders_apple_cli_differences():
    """podman との CLI 差分だけがここにある（共有部は _commands が唯一の情報源）。"""
    net = AC.network_name("abc123")
    cont = AC.container_name("abc123")
    # 命名規則は共有（_commands と同一の関数）。
    assert net == "cube-abc123" and cont == "cube-sb-abc123"
    # ネットワーク: --internal のみ（--disable-dns 相当は無い）。削除は delete が正準形。
    assert AC.create_network_argv(net) == ["network", "create", "--internal", "cube-abc123"]
    assert AC.remove_network_argv(net) == ["network", "delete", "cube-abc123"]
    # run: ホストマウント禁止・`--` 終端子なし・sleep infinity 常駐。
    run = AC.run_container_argv(cont, net, "img")
    assert "-v" not in run and "--volume" not in run and "--mount" not in run
    assert "--" not in run
    assert run[-3:] == ["img", "sleep", "infinity"]
    assert "--network" in run and net in run
    # コンテナ削除は delete --force（podman の rm -f 相当）。
    assert AC.remove_container_argv(cont) == ["delete", "--force", "cube-sb-abc123"]
    assert AC.stop_container_argv(cont) == ["stop", "-t", "1", "cube-sb-abc123"]
    # exec 系・ファイル入出力は podman ビルダと**同一関数**（ウォッチドッグの単一情報源）。
    assert AC.exec_code_argv is C.exec_code_argv
    assert AC.put_file_argv is C.put_file_argv
    assert AC.get_file_argv is C.get_file_argv


def test_create_cleans_up_container_and_network_on_run_failure(tmp_path, darwin26):
    """run 失敗時にコンテナ → ネットワークの順で残骸を掃除して例外を再送出すること。"""
    binary = _fake_container(tmp_path, 'if [ "$1" = run ]; then exit 125; fi\nexit 0')
    d = AppleContainerDriver(binary=binary)
    with pytest.raises(AppleContainerCommandError):
        d.create(template_id="img")
    subcommands = [cmd[1:3] for cmd in d.commands]
    assert subcommands[0] == ["network", "create"]
    assert ["delete", "--force"] in subcommands  # コンテナ残骸の掃除（best-effort）
    assert subcommands[-1] == ["network", "delete"]  # ネットワーク残骸の掃除（最後）


def test_exec_start_wiring_normal_and_cancel(tmp_path, darwin26):
    """共有実行機構（CliExecutionHandle）への配線を fake バイナリで検証する。

    drain・上限・孤児検出の網羅は共有機構のテスト（test_drivers_podman.py——同一クラス）
    が担う。ここでは Apple ドライバ経由でも正常完了とキャンセルが機能することだけを見る。
    """
    import time

    binary = _fake_container(tmp_path, 'if [ "$1" = exec ]; then echo out-line; fi\nexit 0')
    d = AppleContainerDriver(binary=binary, exec_timeout=10.0)
    execution = d.exec("sbx1", "code")
    assert execution.error is None
    assert execution.text == "out-line\n"

    binary2 = _fake_container(tmp_path, 'if [ "$1" = exec ]; then sleep 30; fi\nexit 0')
    d2 = AppleContainerDriver(binary=binary2, exec_timeout=60.0)
    start = time.monotonic()
    handle = d2.exec_start("sbx1", "code")
    handle.cancel()
    execution = handle.result()  # kill 済みのため速やかに返る。
    assert time.monotonic() - start < 5
    assert execution.error is not None
    assert execution.error.name == "Cancelled"


def test_put_get_file_roundtrip_via_fake(tmp_path, darwin26):
    """put_file は stdin 経由・get_file は stdout 経由（ホストマウント非依存）の配線検証。"""
    store = tmp_path / "store.bin"
    # exec サブコマンドの sh -c 本文（put は cat > path / get は cat path）を fake で模擬:
    # stdin をファイルへ、cat 相当はファイルを stdout へ。
    binary = _fake_container(
        tmp_path,
        f'case "$*" in *"cat > "*) cat > "{store}";; *cat*) cat "{store}";; esac\nexit 0',
    )
    d = AppleContainerDriver(binary=binary)
    payload = b"bytes-\x00\x01\xfe"
    d.put_file("sbx1", "/work/f.bin", payload)
    assert d.get_file("sbx1", "/work/f.bin") == payload


def test_get_info_falls_back_to_declared_isolation_level():
    d = AppleContainerDriver()
    info = d.get_info("unknown-sbx")
    assert info.isolation_level is IsolationLevel.VM_PER_CONTAINER


def test_command_error_carries_argv_and_stderr(tmp_path, darwin26):
    binary = _fake_container(
        tmp_path, 'if [ "$1" = network ]; then echo "boom" >&2; exit 7; fi\nexit 0'
    )
    d = AppleContainerDriver(binary=binary)
    with pytest.raises(AppleContainerCommandError) as exc:
        d.create(template_id="img")
    assert exc.value.returncode == 7
    assert "boom" in exc.value.stderr


def test_status_check_runs_once(tmp_path, darwin26):
    """前提チェック（system status）は初回のみ（制御コマンド毎に走らせない）。"""
    counter = tmp_path / "status-count"
    binary = _fake_container(
        tmp_path, f'if [ "$1" = system ]; then echo x >> "{counter}"; fi\nexit 0'
    )
    d = AppleContainerDriver(binary=binary)
    d.create(template_id="img")
    d.create(template_id="img")
    assert counter.read_text().count("x") == 1


def test_exec_timeout_effective_via_shared_machinery(tmp_path, darwin26):
    """ドライバ側ハード上限（共有機構）が Apple ドライバ経由でも実効すること。"""
    binary = _fake_container(tmp_path, 'if [ "$1" = exec ]; then sleep 30; fi\nexit 0')
    d = AppleContainerDriver(binary=binary, exec_timeout=1.0)
    execution = d.exec("sbx1", "code")
    assert execution.error is not None
    assert execution.error.name == "ExecTimeout"


def test_run_rejects_missing_binary_at_popen(darwin26):
    # Popen 時の FileNotFoundError も Unavailable として写像される
    # （binary 明示指定のため前提チェックは通らず、Popen で初めて不在が露見する経路）。
    d = AppleContainerDriver(binary="/nonexistent/container")
    with pytest.raises(AppleContainerUnavailableError):
        d.exec_start("sbx1", "code")


def test_subprocess_status_timeout_maps_to_preflight(tmp_path, darwin26, monkeypatch):
    # status 確認そのものの失敗（タイムアウト等）も runbook 付き前提エラーへ。
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="container system status", timeout=1)

    monkeypatch.setattr(f"{_MOD}.subprocess.run", _boom)
    d = AppleContainerDriver(binary="/usr/local/bin/container")
    with pytest.raises(AppleContainerPreflightError):
        d.create(template_id="img")
