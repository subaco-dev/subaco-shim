"""M2b-1 の実機実測: egress 遮断・サンドボックス間分離・切断キャンセル（Apple Container）。

test_live_podman_isolation.py（M2a-5・Linux + podman nightly で green）の macOS 版。
mock 実測では測れない「実バックエンドでの到達遮断・プロセス停止」を、Apple Silicon
実機の実 Apple Container（軽量 VM）で検証する。M2b-1 の受け入れ条件のうち実測が必要な 3 点:

- **egress 遮断**: ``--internal``（ホスト専用）ネットワークでサンドボックス内から外向き
  TCP に到達できない。
- **サンドボックス間分離**: 同時稼働する A から B の envd(49983) / run_code(49999) ポートへ
  TCP 接続できない（B 内のリスナー実在を positive control で確認したうえで測る）。
- **切断キャンセル**: クライアント切断（handle.cancel()）でコンテナ内の実行プロセスが
  停止する（ハートビートファイルの更新停止で測る——stdin EOF 監視ウォッチドッグが
  Apple Container の exec セッションでも機能するかは実測でしか確定できない）。

実行条件: ``SUBACO_SHIM_LIVE_TEMPLATE`` に pull 可能な OCI 参照（python3 / sh / GNU sleep を
含むイメージ。例 python:3.12-slim、将来は digest 固定の subaco-sandbox）を渡し、かつ
Apple Container が検出されたときのみ走る（macOS 26 / Apple Silicon 実機でのみ実行。
GitHub ホストの macOS ランナーはネスト仮想化が使えないため対象外——セルフホスト
ランナー整備は M2-0）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

import pytest

from subaco_shim.drivers import _commands_apple as AC
from subaco_shim.drivers.apple_container import AppleContainerDriver, _detect_binary

TEMPLATE = os.environ.get("SUBACO_SHIM_LIVE_TEMPLATE")

pytestmark = pytest.mark.skipif(
    TEMPLATE is None or not AppleContainerDriver.available(),
    reason="SUBACO_SHIM_LIVE_TEMPLATE 未設定または Apple Container 未検出（実機でのみ実行）",
)


def _container_binary() -> str:
    return _detect_binary() or "container"


_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}")


def _find_addresses(node: object, out: list[str]) -> None:
    """inspect JSON から IPv4 らしき値を再帰的に集める（gateway 系キーは除外）。

    Apple Container の inspect スキーマは CLI リファレンスに固定形の記載が無いため、
    キー名に依存しすぎない防御的な抽出にする（address/CIDR 表記は prefix を落とす）。
    """
    if isinstance(node, dict):
        for key, val in node.items():
            if "gateway" in key.lower():
                continue
            _find_addresses(val, out)
    elif isinstance(node, list):
        for item in node:
            _find_addresses(item, out)
    elif isinstance(node, str):
        m = _IPV4.match(node.strip())
        if m:
            out.append(node.strip().split("/", 1)[0])


def _container_ip(sandbox_id: str) -> str:
    """サンドボックスコンテナの（自ネットワーク上の）IP を container inspect で得る。"""
    out = subprocess.run(
        [_container_binary(), "inspect", AC.container_name(sandbox_id)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    data = json.loads(out.stdout)
    addrs: list[str] = []
    _find_addresses(data, addrs)
    # 自明な非対象（未割当・ループバック・ブロードキャスト系）を除く。
    addrs = [a for a in addrs if not a.startswith(("0.", "127.", "255."))]
    assert addrs, f"コンテナ IP を inspect から抽出できない: {out.stdout[:2000]}"
    return addrs[0]


def _network_exists(name: str) -> bool:
    out = subprocess.run(
        [_container_binary(), "network", "ls"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    # 名前は cube-<20hex> なので部分一致でも誤検出しない。
    return name in out.stdout


# 外向き TCP プローブ（DNS 非依存の素 IP。到達可否だけを 1 語で報告する）。
def _probe_code(host: str, port: int, timeout: float = 5.0) -> str:
    return (
        "import socket\n"
        "try:\n"
        f"    s = socket.create_connection(({host!r}, {port}), timeout={timeout})\n"
        "    s.close()\n"
        '    print("CONNECTED")\n'
        "except OSError as exc:\n"
        '    print("BLOCKED", type(exc).__name__)\n'
    )


# B 側リスナー（envd/run_code 相当ポートで LISTEN し、READY をファイルで申告する）。
_LISTENER_CODE = """
import socket, threading, time

def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(4)
    s.settimeout(120)
    try:
        while True:
            conn, _ = s.accept()
            conn.close()
    except OSError:
        pass

for port in (49983, 49999):
    threading.Thread(target=serve, args=(port,), daemon=True).start()
with open("/tmp/listener-ready", "w") as f:
    f.write("ready")
time.sleep(120)
"""

# キャンセル実測用ハートビート（0.2 秒間隔で /tmp/beat を更新し続ける）。
_HEARTBEAT_CODE = """
import time
while True:
    with open("/tmp/beat", "w") as f:
        f.write(str(time.time()))
    time.sleep(0.2)
"""


@pytest.fixture(scope="module")
def driver() -> AppleContainerDriver:
    return AppleContainerDriver()


@pytest.fixture
def sandbox(driver):
    info = driver.create(template_id=TEMPLATE)
    try:
        yield info.sandbox_id
    finally:
        driver.destroy(info.sandbox_id)


def _wait_for_file(driver, sandbox_id: str, path: str, deadline_s: float = 30.0) -> bytes:
    """コンテナ内ファイルの出現を待って中身を返す（get_file 経由・ホストマウント非依存）。"""
    deadline = time.monotonic() + deadline_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return driver.get_file(sandbox_id, path)
        except Exception as exc:  # get_file は不在時 AppleContainerCommandError
            last_exc = exc
            time.sleep(0.5)
    raise AssertionError(f"{path} が {deadline_s}s 以内に現れない: {last_exc}")


def test_egress_blocked_by_default(driver, sandbox):
    # 既定のホスト専用（--internal）ネットワークでは外向き TCP（素 IP・DNS 非依存）に
    # 到達できないこと。
    ex = driver.exec(sandbox, _probe_code("1.1.1.1", 443))
    assert ex.error is None, f"プローブ自体が失敗: {ex.error}"
    assert ex.text is not None and ex.text.startswith("BLOCKED"), (
        f"egress が遮断されていない: {ex.text!r}"
    )


def test_sandbox_to_sandbox_ports_blocked(driver):
    # 同時稼働する A から B の envd(49983) / run_code(49999) へ TCP 接続できないこと。
    a = driver.create(template_id=TEMPLATE)
    b = driver.create(template_id=TEMPLATE)
    listener = None
    try:
        listener = driver.exec_start(b.sandbox_id, _LISTENER_CODE)
        _wait_for_file(driver, b.sandbox_id, "/tmp/listener-ready")

        # positive control: B 自身からは両ポートに到達できる（リスナー実在の証明。
        # これがないと「そもそも LISTEN していないから接続失敗」と区別できない）。
        for port in (49983, 49999):
            ex = driver.exec(b.sandbox_id, _probe_code("127.0.0.1", port))
            assert ex.text is not None and ex.text.startswith("CONNECTED"), (
                f"positive control 失敗（B 内リスナー :{port} に B 自身が届かない）: "
                f"{ex.text!r} logs={ex.logs}"
            )

        # 本測定: A から B のネットワーク上 IP へは両ポートとも到達できない。
        b_ip = _container_ip(b.sandbox_id)
        for port in (49983, 49999):
            ex = driver.exec(a.sandbox_id, _probe_code(b_ip, port))
            assert ex.error is None, f"プローブ自体が失敗: {ex.error}"
            assert ex.text is not None and ex.text.startswith("BLOCKED"), (
                f"サンドボックス間が遮断されていない（A → B:{port}）: {ex.text!r}"
            )
    finally:
        if listener is not None:
            listener.cancel()
        driver.destroy(a.sandbox_id)
        driver.destroy(b.sandbox_id)

    # destroy 後にサンドボックス個別ネットワークの残骸が残らないこと（実測版）。
    for sid in (a.sandbox_id, b.sandbox_id):
        assert not _network_exists(AC.network_name(sid)), (
            f"ネットワーク残骸: {AC.network_name(sid)}"
        )


def test_cancel_stops_in_container_process(driver, sandbox):
    # クライアント切断（cancel）でコンテナ内の実行プロセスが実際に停止すること。
    # stdin EOF 監視ウォッチドッグ（podman nightly で確定した経路）が Apple Container の
    # exec セッションでも機能するかは実測でしか確定できない。
    handle = driver.exec_start(sandbox, _HEARTBEAT_CODE)
    _wait_for_file(driver, sandbox, "/tmp/beat")

    handle.cancel()
    result = handle.result()
    assert result.error is not None and result.error.name == "Cancelled"

    # ハートビートが止まったことを 2 点観測で確認する（更新中なら中身が変わる）。
    time.sleep(2.0)
    first = driver.get_file(sandbox, "/tmp/beat")
    time.sleep(2.0)
    second = driver.get_file(sandbox, "/tmp/beat")
    assert first == second, (
        "cancel 後もコンテナ内プロセスが生きている（/tmp/beat が更新され続けている）"
    )
