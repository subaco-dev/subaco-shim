"""サンドボックス間ネットワーク分離の意図を単体で表明する。

実 podman を使わず、mock ドライバが :mod:`subaco_shim.drivers._commands` 経由で記録する
podman 風 argv 列から、次の設計上の不変条件を検証する:

- **サンドボックスごとに個別の egress なし内部ネットワークを作成**（``network create --internal
  cube-<id>``）。``--network=none`` は使わない（ホスト → データプレーンの TCP 到達性は維持）。
- 各コンテナは **自分のネットワークにのみ接続**し、他サンドボックスのネットワークには接続しない。
  これがサンドボックス A から B への L3 到達を成立させない（= 相互遮断の）意図である。
- **ホストディレクトリマウント禁止**（``-v`` を一切付けない）。
- **destroy 時にそのサンドボックスのネットワーク残骸のみを掃除**し、他は生存させる。
  全破棄後にネットワークリークが残らない。

実バックエンドでの到達遮断 e2e（A から B への接続失敗確認）は実機統合で検証する。
本テストはその設計意図をコマンド列レベルで固定する回帰ガードである。
"""

from __future__ import annotations

from subaco_shim.drivers import _commands as C
from subaco_shim.drivers.mock import MockDriver


def _run_argv_for(driver: MockDriver, network: str) -> list[list[str]]:
    """指定ネットワークへ接続する run コマンド（フル argv）を抽出する。"""
    return [c for c in driver.commands if len(c) > 1 and c[1] == "run" and network in c]


def test_each_sandbox_gets_distinct_internal_network():
    d = MockDriver()
    sid_a = d.create(template_id="tmpl").sandbox_id
    sid_b = d.create(template_id="tmpl").sandbox_id
    net_a, net_b = C.network_name(sid_a), C.network_name(sid_b)

    # サンドボックスごとに別個のネットワーク（A と B は異なるネットワーク名）。
    assert net_a != net_b
    assert {net_a, net_b} <= set(d.created_networks)
    assert {net_a, net_b} <= d.live_networks

    # egress なし内部ネットワークとして作成（--internal を使い、--network=none は使わない）。
    for net in (net_a, net_b):
        assert [C.PODMAN, "network", "create", "--internal", net] in d.commands


def test_no_none_network_and_no_host_mount():
    # どのコマンドにも --network=none / -v（ホストマウント）が現れないこと。
    d = MockDriver()
    d.create(template_id="tmpl")
    for cmd in d.commands:
        assert "none" not in cmd  # --network=none は不使用（データプレーン到達性維持）。
        assert "-v" not in cmd  # ホストディレクトリマウント禁止。


def test_container_attaches_only_to_own_network():
    # A のコンテナは A のネットワークにのみ接続し、B のネットワークには接続しない。
    # = A から B への L3 到達が成立しない意図（サンドボックス間はネットワークで相互遮断）。
    d = MockDriver()
    sid_a = d.create(template_id="tmpl").sandbox_id
    sid_b = d.create(template_id="tmpl").sandbox_id
    net_a, net_b = C.network_name(sid_a), C.network_name(sid_b)

    run_a = _run_argv_for(d, net_a)
    run_b = _run_argv_for(d, net_b)
    assert len(run_a) == 1 and len(run_b) == 1
    assert net_b not in run_a[0]  # A のコンテナは B のネットワークに参加しない。
    assert net_a not in run_b[0]  # B のコンテナは A のネットワークに参加しない。


def test_network_created_before_container_start():
    # 到達遮断を成立させるため、コンテナ起動より前にネットワークを作成する順序。
    d = MockDriver()
    sid = d.create(template_id="tmpl").sandbox_id
    net = C.network_name(sid)
    create_idx = d.commands.index([C.PODMAN, "network", "create", "--internal", net])
    run_idx = d.commands.index(_run_argv_for(d, net)[0])
    assert create_idx < run_idx


def test_destroy_cleans_only_target_network():
    # destroy はそのサンドボックスのネットワークのみ掃除し、他は生存させる。
    d = MockDriver()
    sid_a = d.create(template_id="tmpl").sandbox_id
    sid_b = d.create(template_id="tmpl").sandbox_id
    net_a, net_b = C.network_name(sid_a), C.network_name(sid_b)

    d.destroy(sid_a)
    assert net_a in d.removed_networks
    assert [C.PODMAN, "network", "rm", net_a] in d.commands
    assert net_a not in d.live_networks
    assert net_b in d.live_networks  # B のネットワークは掃除対象外。

    d.destroy(sid_b)
    assert net_b in d.removed_networks
    assert not d.live_networks  # 全破棄後にネットワークリークが残らない。
