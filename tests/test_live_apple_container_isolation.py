"""M2b-1 の実機実測: egress 遮断・サンドボックス間分離・切断キャンセル（Apple Container）。

test_live_podman_isolation.py（M2a-5・Linux + podman nightly で green）の macOS 版。
mock 実測では測れない「実バックエンドでの到達遮断・プロセス停止」を、Apple Silicon
実機の実 Apple Container（軽量 VM）で検証する。M2b-1 の受け入れ条件のうち実測が必要な 3 点:

- **egress 遮断**: ``--internal``（ホスト専用）ネットワークでサンドボックス内から外向き
  TCP に到達できない。
- **サンドボックス間分離**: 同時稼働する A から B の envd(49983) / run_code(49999) ポートへ
  TCP 接続できない（B 内のリスナー実在を positive control で確認したうえで測る）。
  **IPv4 に加え IPv6（ULA）でも測る**——Apple Container はコンテナへ IPv6 も付与するため、
  v4 遮断だけでは分離の証明として不足する（podman の内部ネットワークは既定 v4 のみ）。
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
# ULA（fc00::/7）のみを対象にする。link-local（fe80::）はスコープ指定が要るため別扱い。
_IPV6_ULA = re.compile(r"^f[cd][0-9a-f]{2}:[0-9a-f:]+$", re.IGNORECASE)


def _find_addresses(node: object, out: list[str], pattern: re.Pattern[str]) -> None:
    """inspect JSON からパターンに合う IP らしき値を再帰的に集める（gateway 系キーは除外）。

    Apple Container の inspect スキーマは CLI リファレンスに固定形の記載が無く、
    実測でも networks/state がトップレベルに載る形と ``status`` 配下に載る形の揺れを
    観測したため、キー位置に依存しない防御的な抽出にする（CIDR 表記は prefix を落とす）。
    """
    if isinstance(node, dict):
        for key, val in node.items():
            if "gateway" in key.lower():
                continue
            _find_addresses(val, out, pattern)
    elif isinstance(node, list):
        for item in node:
            _find_addresses(item, out, pattern)
    elif isinstance(node, str):
        bare = node.strip().split("/", 1)[0]
        if pattern.match(bare):
            out.append(bare)


def _inspect_addresses(sandbox_id: str, pattern: re.Pattern[str]) -> list[str]:
    out = subprocess.run(
        [_container_binary(), "inspect", AC.container_name(sandbox_id)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    addrs: list[str] = []
    _find_addresses(json.loads(out.stdout), addrs, pattern)
    return addrs


def _container_ip(sandbox_id: str) -> str:
    """サンドボックスコンテナの（自ネットワーク上の）IPv4 を container inspect で得る。"""
    addrs = _inspect_addresses(sandbox_id, _IPV4)
    # 自明な非対象（未割当・ループバック・ブロードキャスト系）を除く。
    addrs = [a for a in addrs if not a.startswith(("0.", "127.", "255."))]
    assert addrs, f"コンテナ IPv4 を inspect から抽出できない: {sandbox_id}"
    return addrs[0]


def _container_ipv6(sandbox_id: str) -> str:
    """サンドボックスコンテナの ULA IPv6 を container inspect で得る。

    実測: 各ネットワークはネットワークごとに**別の**ランダム ULA /64 prefix を持ち、
    コンテナへ ULA アドレスが 1 つ付与される（fe80:: link-local とは別）。
    """
    addrs = _inspect_addresses(sandbox_id, _IPV6_ULA)
    assert addrs, f"コンテナ ULA IPv6 を inspect から抽出できない: {sandbox_id}"
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

# B 側 IPv6 リスナー（ULA を含む全 v6 アドレスで LISTEN し、READY をファイルで申告する）。
_LISTENER_V6_CODE = """
import socket, time
s = socket.socket(socket.AF_INET6)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("::", 49999))
s.listen(4)
with open("/tmp/listener6-ready", "w") as f:
    f.write("ready")
s.settimeout(120)
try:
    while True:
        conn, _ = s.accept()
        conn.close()
except OSError:
    pass
"""

# v6 の positive control: 自分の link-local へ自己接続する（v6 スタック + リスナー実在の証明）。
# link-local は SLAAC（RA 待ち）に依存せず interface up で即時付与される（DAD 数秒のみ）ため
# 決定的。ULA の自己接続を使わない理由は本測定側のコメント参照。
_V6_SELF_PROBE = """
import socket, time
deadline = time.monotonic() + 30
result = "NO-LINKLOCAL"
while time.monotonic() < deadline:
    ll = None
    for line in open("/proc/net/if_inet6"):
        parts = line.split()
        if parts[-1] != "lo" and parts[0].startswith("fe80"):
            raw = parts[0]
            ll = ":".join(raw[i:i + 4] for i in range(0, 32, 4))
            break
    if ll is not None:
        try:
            s = socket.create_connection((ll + "%eth0", 49999), timeout=5)
            s.close()
            result = "CONNECTED"
            break
        except OSError as exc:
            result = "BLOCKED " + type(exc).__name__
    time.sleep(1.0)
print(result)
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


def test_sandbox_to_sandbox_ipv6_blocked(driver):
    """A → B は IPv4 だけでなく **IPv6 でも**遮断されること（v4 遮断だけでは分離の証明に不足）。

    実測（2026-08-26）: コンテナにはネットワークごとに**別 prefix** の ULA IPv6 が
    SLAAC（RA 受信）で付与され、inspect の ``ipv6Address`` はその**ホスト側割当値**
    （prefix + EUI-64）を SLAAC 完了前から返す。A → B の遮断は **A 側ルーティング表の
    性質**（自 /64 以外への経路・default 経路なし → ENETUNREACH）なので、B の SLAAC
    状態に依存せず決定的に測れる。手動実測では**双方の ULA が完全に立った状態**でも
    A → B ULA = ENETUNREACH・link-local = L2 分離でタイムアウトを確認済み。

    positive control に ULA 自己接続を使わないのは、2 枚目以降のネットワークで RA が
    数分単位で遅れることを実測したため（初期 Router Solicitation の取りこぼし後は
    周期 RA 待ちになり、コンテナ内から RS を再送しても誘発できなかった）。link-local
    自己接続（即時付与・RA 非依存）で v6 スタックとリスナー実在を証明する。
    """
    a = driver.create(template_id=TEMPLATE)
    b = driver.create(template_id=TEMPLATE)
    listener = None
    try:
        listener = driver.exec_start(b.sandbox_id, _LISTENER_V6_CODE)
        _wait_for_file(driver, b.sandbox_id, "/tmp/listener6-ready")
        b_ip6 = _container_ipv6(b.sandbox_id)

        # positive control: B 自身から自分の link-local へ（v6 スタック + リスナー実在の証明）。
        ex = driver.exec(b.sandbox_id, _V6_SELF_PROBE)
        assert ex.error is None, f"positive control の実行自体が失敗: {ex.error}"
        assert ex.text is not None and ex.text.startswith("CONNECTED"), (
            f"positive control 失敗（B 自身が自 link-local:49999 に届かない）: {ex.text!r}"
        )

        # 本測定 1: A から B の（ホスト割当）ULA へは到達できない（A に経路がない）。
        ex = driver.exec(a.sandbox_id, _probe_code(b_ip6, 49999))
        assert ex.error is None, f"プローブ自体が失敗: {ex.error}"
        assert ex.text is not None and ex.text.startswith("BLOCKED"), (
            f"サンドボックス間が IPv6 で遮断されていない（A → B [{b_ip6}]:49999）: {ex.text!r}"
        )

        # 本測定 2: 外向き IPv6（素 IP・DNS 非依存）にも到達できない。
        ex = driver.exec(a.sandbox_id, _probe_code("2606:4700:4700::1111", 443))
        assert ex.error is None, f"プローブ自体が失敗: {ex.error}"
        assert ex.text is not None and ex.text.startswith("BLOCKED"), (
            f"IPv6 egress が遮断されていない: {ex.text!r}"
        )
    finally:
        if listener is not None:
            listener.cancel()
        driver.destroy(a.sandbox_id)
        driver.destroy(b.sandbox_id)


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
