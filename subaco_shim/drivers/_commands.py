"""podman サブコマンド argv の純関数ビルダとサンドボックス命名規則。

このモジュールは **stdlib のみ**に依存し、podman バイナリが無くても import・実行できる。
podman ドライバ（:mod:`subaco_shim.drivers.podman`）と契約テスト用モックドライバ
（:mod:`subaco_shim.drivers.mock`）の**両方**がここを唯一の情報源として使い、
「ネットワークの個別作成／掃除」等のコマンド列を一致させる（モックで検証可能にする）。

設計上の要点:

- 未信頼コードは **egress を持たない内部ネットワーク**で実行する。podman では
  ``podman network create --internal`` を用い、``--network=none`` は使わない
  （ホスト → データプレーン〔envd 49983 / run_code 49999〕への TCP 到達性を維持するため）。
- **内部ネットワークはサンドボックスごとに個別作成**（``cube-<sandbox_id>``）して
  サンドボックス間の相互到達を遮断する。破棄時にネットワーク残骸を掃除する。
- **ホストディレクトリのマウントは禁止**（``-v`` を一切付けない）。ファイル入出力は
  ``podman exec`` 経由の stdin/stdout（put_file / get_file）に限定する。

TODO: ネットワーク間分離の enforce（``-o isolate=true`` の要否）と、run_code の
真のトランスポート（envd:49999 Connect RPC/HTTP）は spike で確定。ここでは v0 骨子として
``podman exec python3 -c`` でコードを実行する薄い経路を提供する。
"""

from __future__ import annotations

import posixpath
import shlex

# podman バイナリの既定名（システムインストール優先検出は podman ドライバが行う）。
PODMAN = "podman"

# サンドボックス個別ネットワーク名の接頭辞（例 ``cube-<sandbox_id>``）。
NETWORK_PREFIX = "cube-"
# コンテナ名の接頭辞（ネットワーク名と衝突しないよう別接頭辞にする）。
CONTAINER_PREFIX = "cube-sb-"


def network_name(sandbox_id: str) -> str:
    """サンドボックス個別ネットワーク名を返す（``cube-<sandbox_id>``）。"""
    return f"{NETWORK_PREFIX}{sandbox_id}"


def container_name(sandbox_id: str) -> str:
    """サンドボックスのコンテナ名を返す。"""
    return f"{CONTAINER_PREFIX}{sandbox_id}"


# --- ネットワーク（サンドボックスごとに個別作成／破棄時に掃除） -------


def create_network_argv(name: str) -> list[str]:
    """egress なし内部ネットワークを作成する argv（``--internal``。``--network=none`` は使わない）。

    TODO: ネットワーク間分離の enforce に ``-o isolate=true`` を付すかは spike で確定。
    """
    return ["network", "create", "--internal", name]


def remove_network_argv(name: str) -> list[str]:
    """ネットワークを削除する argv（destroy 時のネットワーク残骸掃除）。"""
    return ["network", "rm", name]


# --- コンテナ（ホストマウント禁止・個別ネットワークへ接続） -----------


def run_container_argv(container: str, network: str, image: str) -> list[str]:
    """サンドボックスコンテナを起動する argv（ホストマウントなし・個別ネットワーク接続）。

    ``-v``（ホストディレクトリマウント）は**一切付けない**。
    v0 骨子ではコンテナを常駐させるため ``sleep infinity`` で起動し、exec でコードを流す。
    TODO: 実テンプレートは envd/run_code サービスを常駐させる。その起動形は spike で確定。
    """
    return [
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        # NOTE: -v によるホストマウントは禁止。ここに -v を足さないこと。
        "--",
        image,
        "sleep",
        "infinity",
    ]


def exec_code_argv(container: str, code: str) -> list[str]:
    """コンテナ内でコードを実行する argv（E2B run_code 相当の v0 骨子）。

    TODO: 実際の run_code は envd:49999 のトランスポート（Connect RPC/HTTP/SSE 等）で
    実行される。ここでは骨子として ``python3 -c`` に直接流す薄い経路。
    """
    return ["exec", "-i", container, "python3", "-c", code]


def put_file_argv(container: str, path: str) -> list[str]:
    """ホスト側 bytes をコンテナ内 ``path`` へ書き込む argv（stdin 経由。ホストマウント不使用）。

    E2B の files.write と同じく**親ディレクトリを自動作成**する（envd 実装は書き込み時に
    ディレクトリを掘る。これがないと実イメージへの ``/work/...`` 等の書き込みが失敗する）。
    """
    parent = posixpath.dirname(path) or "."
    return [
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(path)}",
    ]


def get_file_argv(container: str, path: str) -> list[str]:
    """コンテナ内 ``path`` を stdout へ取り出す argv（ホストマウント不使用）。"""
    return ["exec", container, "cat", "--", path]


def stop_container_argv(container: str) -> list[str]:
    """コンテナを停止する argv（destroy 時）。"""
    return ["stop", "-t", "1", container]


def remove_container_argv(container: str) -> list[str]:
    """コンテナを強制削除する argv（destroy 時）。"""
    return ["rm", "-f", container]


def full_argv(binary: str, subargv: list[str]) -> list[str]:
    """podman バイナリ名とサブコマンド argv を結合したフル argv を返す。"""
    return [binary, *subargv]
