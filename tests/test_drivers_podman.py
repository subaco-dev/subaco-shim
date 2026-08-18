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
    PodmanCommandError,
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


def test_detect_binary_prefers_path_over_system_paths(monkeypatch):
    """PATH の podman をシステム既知パスより優先すること（CI 実測ハングの再発防止）。

    複数インストール環境（GitHub ランナー: /usr/local 同梱 5.x + apt の /usr/bin 4.9）で
    ストレージ所有者（= PATH 解決される個体）と別の podman を選ぶと、バージョン混在の
    ストレージアクセスで `podman run` が futex 待ちハングする。
    """
    from subaco_shim.drivers import podman as P

    monkeypatch.setattr(P.shutil, "which", lambda name: "/custom/bin/podman")
    # システム既知パスがすべて「存在・実行可能」でも PATH 側が勝つ。
    monkeypatch.setattr(P.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(P.os, "access", lambda p, m: True)
    assert P._detect_binary() == "/custom/bin/podman"


def test_detect_binary_falls_back_to_system_paths(monkeypatch):
    # PATH に無ければシステム既知パスへフォールバックする。
    from subaco_shim.drivers import podman as P

    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    monkeypatch.setattr(P.os.path, "isfile", lambda p: p == "/usr/local/bin/podman")
    monkeypatch.setattr(P.os, "access", lambda p, m: True)
    assert P._detect_binary() == "/usr/local/bin/podman"


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
    assert C.create_network_argv(net) == [
        "network",
        "create",
        "--internal",
        "--disable-dns",
        "cube-abc123",
    ]
    assert C.remove_network_argv(net) == ["network", "rm", "cube-abc123"]
    run = C.run_container_argv(C.container_name("abc123"), net, "img")
    assert "-v" not in run  # ホストマウント禁止。
    assert "--network" in run and net in run
    assert C.get_file_argv("cube-sb-abc123", "/p") == ["exec", "cube-sb-abc123", "cat", "--", "/p"]
    # put_file は E2B files.write と同じく親ディレクトリを自動作成する（実イメージには
    # /work 等が存在しないため、mkdir -p がないと実機統合の書き込みが失敗する）。
    put = C.put_file_argv("cube-sb-abc123", "/work dir/契約.bin")
    assert put[:4] == ["exec", "-i", "cube-sb-abc123", "sh"]
    assert put[-1] == "mkdir -p '/work dir' && cat > '/work dir/契約.bin'"
    # 親なし相対パスは "." を掘る（無害な no-op）。
    assert C.put_file_argv("c", "f.txt")[-1] == "mkdir -p . && cat > f.txt"
    # exec は stdin 監視ウォッチドッグ付き sh ラッパー（切断キャンセルのコンテナ内到達。
    # podman exec クライアントの kill だけではコンテナ内プロセスが生き残る——nightly 実測）。
    ex = C.exec_code_argv("cube-sb-abc123", "print('hi')")
    assert ex[:5] == ["exec", "-i", "cube-sb-abc123", "sh", "-c"]
    wrapper = ex[5]
    assert "python3 -c 'print('\"'\"'hi'\"'\"')' </dev/null" in wrapper  # payload は quote 済み
    assert "cat >/dev/null" in wrapper and "kill -9" in wrapper  # stdin EOF 監視
    assert wrapper.rstrip().endswith('wait "$pid"')  # 終了コードは payload のものを返す


def test_create_cleans_up_container_and_network_on_run_failure(tmp_path):
    """run 失敗時にコンテナ → ネットワークの順で残骸を掃除して例外を再送出すること。

    実測: CI で `podman run -d --network <internal>` がタイムアウトした場合でも
    コンテナ記録が作られていることがある。従来はネットワークしか掃除せず、
    コンテナ残骸（cube-sb-<id>）がリークした。
    """
    fake = tmp_path / "fake-podman"
    fake.write_text('#!/bin/sh\nif [ "$1" = run ]; then exit 125; fi\nexit 0\n')
    fake.chmod(0o755)
    d = PodmanDriver(binary=str(fake))
    with pytest.raises(PodmanCommandError):
        d.create(template_id="img")
    subcommands = [cmd[1:3] for cmd in d.commands]
    assert subcommands[0] == ["network", "create"]
    assert ["rm", "-f"] in subcommands  # コンテナ残骸の掃除（best-effort）
    assert subcommands[-1] == ["network", "rm"]  # ネットワーク残骸の掃除（最後）


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


def test_exec_orphaned_children_killed_and_not_success(tmp_path):
    """親が正常終了しても子孫が残る場合、グループごと停止し**成功扱いにしない**。

    レビュー指摘の再現: 親が sleep 30 の子（stdout 継承）を残して exit 0 すると、
    従来は reader 待ちの後に error=None（成功）で返り、子孫はハード上限も受けずに
    走り続けた。
    """
    import time

    binary = _fake_podman(tmp_path, "sleep 30 &\nexit 0")
    d = PodmanDriver(binary=binary, exec_timeout=60.0)
    start = time.monotonic()
    execution = d.exec("sbx1", "code")
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"子孫の sleep 30 を待ってはならない: {elapsed:.1f}s"
    assert execution.error is not None, "子孫が残った実行を成功扱いにしてはならない"
    assert execution.error.name == "OrphanedProcesses"


def test_exec_orphaned_devnull_children_detected_and_killed(tmp_path):
    """stdio を /dev/null へ向けた子孫（pipe 非保持）も検出・停止する。

    レビュー指摘の再現: pipe の EOF は「write 端を保持する子孫がいない」ことしか
    示さないため、stdio を切り離した子孫は 0.04 秒で error=None（成功）のまま
    生き残った。プロセスグループの生存確認で検出し、PID 消滅まで停止する。
    """
    import os
    import time

    pidfile = tmp_path / "child.pid"
    binary = _fake_podman(
        tmp_path,
        f'sleep 30 >/dev/null 2>&1 </dev/null &\necho $! > "{pidfile}"\nexit 0',
    )
    d = PodmanDriver(binary=binary, exec_timeout=60.0)
    start = time.monotonic()
    execution = d.exec("sbx1", "code")
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"子孫の sleep 30 を待ってはならない: {elapsed:.1f}s"
    assert execution.error is not None, "子孫が残った実行を成功扱いにしてはならない"
    assert execution.error.name == "OrphanedProcesses"
    # kill シグナルの送付だけでなく、子孫 PID が実際に消滅していること。
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"orphaned child pid={pid} still alive")


def test_exec_line_splitting_is_bounded(tmp_path):
    """バイト上限通過後の行分割にも上限がある（短い行の大量分割による再膨張の防止）。

    レビュー指摘の再現: 10MiB の "abcd\\n" は splitlines() で約 210 万要素・
    最大 RSS 約 210MB に膨張した。行イベント数を上限で抑え、残りは 1 要素に集約する。
    """
    # 1MiB 上限で "abcd\n" を 2MiB 出力 → 蓄積は 1MiB（約 21 万行相当）。
    binary = _fake_podman(tmp_path, "yes abcd | head -c 2097152")
    d = PodmanDriver(binary=binary, exec_timeout=30.0, exec_max_output=1024 * 1024)
    execution = d.exec("sbx1", "code")
    from subaco_shim.drivers.podman import _MAX_OUTPUT_LINES

    # 行イベント数は上限 + 集約された残り 1 要素以内。
    assert len(execution.logs.stdout) <= _MAX_OUTPUT_LINES + 1
    # データは集約要素に保持されている（総量はバイト上限のオーダー）。
    total = sum(len(line) for line in execution.logs.stdout)
    assert total <= 1024 * 1024


def test_split_lines_bounded_semantics():
    from subaco_shim.drivers.podman import _split_lines_bounded

    # 上限内は splitlines() 相当（末尾改行で空要素を作らない）。
    assert _split_lines_bounded("a\nb\n", max_lines=10) == ["a", "b"]
    assert _split_lines_bounded("a\nb", max_lines=10) == ["a", "b"]
    assert _split_lines_bounded("", max_lines=10) == []
    # 上限超過は先頭 N 行 + 残り 1 要素（データは失わない）。
    bounded = _split_lines_bounded("1\n2\n3\n4\n5\n", max_lines=2)
    assert bounded == ["1", "2", "3\n4\n5\n"]


def test_split_lines_bounded_matches_splitlines():
    """上限内の結果は str.splitlines() と**同一**（LF 以外の全境界を含む）。

    レビュー指摘の再現: LF 限定の分割では "a\\r\\nb\\r\\n" が従来の ["a", "b"] でなく
    ["a\\r", "b\\r"] になった。CR/CRLF/VT/FF/FS/GS/RS/NEL/LS/PS を splitlines() と
    同じ境界として扱う。
    """
    from subaco_shim.drivers.podman import _split_lines_bounded

    samples = [
        "a\r\nb\r\n",
        "a\rb",
        "a\vb\fc",
        "a\x1cb\x1dc\x1ed",
        "a\x85b",
        "a\u2028b\u2029c",
        "\n\n",
        "no newline",
        "trailing\n",
        "\r\n",
        "\r\r\n\n",
        "mixed\r\nof\rall\n\vkinds\u2028end\u2029",
    ]
    for s in samples:
        assert _split_lines_bounded(s, max_lines=100) == s.splitlines(), repr(s)
    # 上限超過時も境界の扱いは同一（CRLF は 1 境界。残りは境界ごと 1 要素に集約）。
    assert _split_lines_bounded("a\r\nb\r\nc\r\nd\r\n", max_lines=2) == ["a", "b", "c\r\nd\r\n"]


def test_env_limit_values_reject_non_finite_and_fractional(tmp_path, monkeypatch):
    """nan/inf・小数の制限値は受理せず既定値へフォールバックする。"""
    from subaco_shim.drivers.podman import (
        _DEFAULT_EXEC_MAX_OUTPUT,
        _DEFAULT_EXEC_TIMEOUT,
        resolve_exec_max_output,
        resolve_exec_timeout,
    )

    for bad in ("nan", "inf", "-inf", "abc"):
        monkeypatch.setenv("SUBACO_SHIM_EXEC_TIMEOUT", bad)
        assert resolve_exec_timeout() == _DEFAULT_EXEC_TIMEOUT, bad
    for bad in ("nan", "inf", "0.5", "abc"):
        monkeypatch.setenv("SUBACO_SHIM_EXEC_MAX_OUTPUT", bad)
        assert resolve_exec_max_output() == _DEFAULT_EXEC_MAX_OUTPUT, bad
    # 正常値と無効化（0 以下）は従来どおり。
    monkeypatch.setenv("SUBACO_SHIM_EXEC_TIMEOUT", "12")
    assert resolve_exec_timeout() == 12.0
    monkeypatch.setenv("SUBACO_SHIM_EXEC_TIMEOUT", "0")
    assert resolve_exec_timeout() is None
    monkeypatch.setenv("SUBACO_SHIM_EXEC_MAX_OUTPUT", "1024")
    assert resolve_exec_max_output() == 1024
    monkeypatch.setenv("SUBACO_SHIM_EXEC_MAX_OUTPUT", "0")
    assert resolve_exec_max_output() is None
