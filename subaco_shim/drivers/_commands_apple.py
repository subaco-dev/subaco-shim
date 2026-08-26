"""Apple Container（``container`` CLI）サブコマンド argv の純関数ビルダ。

:mod:`._commands`（podman）と対をなす **stdlib のみ**のビルダ。命名規則
（``cube-<id>`` / ``cube-sb-<id>``）・stdin 監視ウォッチドッグ・exec 経由の
ファイル入出力は :mod:`._commands` を唯一の情報源として共有し、**CLI 差分だけ**を
ここで持つ。差分（Apple Container v1.0+ の CLI リファレンスで確認・実機実測は
live スイート）:

- ネットワーク作成は ``network create --internal <name>``。``--internal`` は
  「ホスト専用ネットワークに制限」＝ egress 遮断とホスト → データプレーン到達維持を
  1 オプションで満たす（podman の ``--internal --disable-dns`` に対応。per-network の
  DNS デーモンという可動部がそもそも無いため ``--disable-dns`` 相当は不要）。
- ネットワーク削除は ``network delete``（``rm`` はエイリアス——正準形を使う）。
- コンテナ削除は ``delete --force``（podman の ``rm -f`` 相当。``rm`` はエイリアス）。
- ``run`` の positional は ``<image> [<arguments> ...]``。podman ビルダが防御的に
  付ける ``--`` 終端子は付けない（swift-argument-parser 系 CLI での解釈差を避ける。
  渡す値に ``-`` 始まりは無い）。

設計上の要点（podman と同一）: 未信頼コードは egress を持たない内部ネットワークで
実行し、**内部ネットワークはサンドボックスごとに個別作成**して相互到達を遮断する。
**ホストディレクトリのマウントは禁止**（``--volume``/``--mount`` を一切付けない）。
"""

from __future__ import annotations

# 共有部（命名規則・ウォッチドッグ・exec 経由ファイル入出力・argv 結合）は
# _commands が唯一の情報源。exec 系サブコマンドの形（exec -i / sh -c / cat）は
# Docker 互換 CLI で共通のため、関数ごと再利用する。
from ._commands import (
    container_name,
    exec_code_argv,
    full_argv,
    get_file_argv,
    network_name,
    put_file_argv,
)

__all__ = [
    "container_name",
    "create_network_argv",
    "exec_code_argv",
    "full_argv",
    "get_file_argv",
    "network_name",
    "put_file_argv",
    "remove_container_argv",
    "remove_network_argv",
    "run_container_argv",
    "stop_container_argv",
]

# Apple Container の CLI 名（v1.0 で CLI/API 凍結済み）。
CONTAINER_CLI = "container"


def create_network_argv(name: str) -> list[str]:
    """egress なしホスト専用ネットワークを作成する argv（``--internal``）。

    ``--internal`` は「Restrict to host-only network」——サンドボックスから外向き経路を
    持たず、ホスト → データプレーン（envd 49983 / run_code 49999）の TCP 到達性は残る
    想定（実機実測は live スイートの egress 遮断・positive control で確認する）。
    """
    return ["network", "create", "--internal", name]


def remove_network_argv(name: str) -> list[str]:
    """ネットワークを削除する argv（destroy 時のネットワーク残骸掃除）。"""
    return ["network", "delete", name]


def run_container_argv(container: str, network: str, image: str) -> list[str]:
    """サンドボックスコンテナ（= 個別軽量 VM）を起動する argv。

    ``--volume``/``--mount``（ホストディレクトリマウント）は**一切付けない**。
    コンテナを常駐させるため ``sleep infinity`` で起動し、exec でコードを流す
    （イメージに GNU sleep が必要——podman ドライバと同じテンプレート契約）。
    """
    return [
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        # NOTE: --volume/--mount によるホストマウントは禁止。ここに足さないこと。
        image,
        "sleep",
        "infinity",
    ]


def stop_container_argv(container: str) -> list[str]:
    """コンテナを停止する argv（destroy 時。``-t`` は猶予秒——podman と同形）。"""
    return ["stop", "-t", "1", container]


def remove_container_argv(container: str) -> list[str]:
    """コンテナを強制削除する argv（destroy 時。``delete --force`` が正準形）。"""
    return ["delete", "--force", container]
