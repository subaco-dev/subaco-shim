"""E2B ワイヤプロトコルの面（プレーン・オペレーション・ヘッダ・ポート）。

**この層は v0 骨子であり、E2B の忠実再現は最重要 spike 事項である。** 本モジュールは
「シムが実装すべき操作の集合」と「各操作がどのプレーンに属し、どの認証ヘッダを要求するか」を
固定するだけで、実バイト列（Connect RPC/protobuf・サブドメイン多重化・TLS）は扱わない。

E2B の 2 プレーン:

- **制御プレーン**: サンドボックスの create/destroy・get_info。SDK は各要求に
  ``X-API-KEY`` を必ず送るため、シムはこれを検証すれば足りる（クライアント無改修）。
- **envd データプレーン**: files（ポート ``49983``）と run_code（ポート ``49999``）。
  ``{port}-{sandbox_id}.{sandbox_domain}`` というサブドメイン形式・既定 https で接続する。
  envd 経路は ``X-API-KEY`` を運ばないため、create 応答で返す **envd アクセストークン**を
  SDK が ``X-Access-Token`` として自動付与する。シムはこれを全 envd 経路で検証する。

**v0 スコープ（5 系統）**: create/destroy・run_code・files read/write・get_info。

TODO(最重要 spike): 以下は本骨子では未再現。忠実再現の可否・工数を spike で確定し、
過大なら薄い自前クライアント（subaco SDK）へ倒す。
  - envd（49983）の **Connect RPC / Protocol Buffers**（``spec/envd/`` の .proto）面。
  - run_code（49999）の実トランスポート（HTTP/SSE/gRPC/WebSocket）とストリーミング挙動
    （固定する e2b-code-interpreter バージョンで要確認）。
  - ``sandbox_domain`` のローカル解決（ワイルドカード DNS / sslip.io / ホストベースルーティング）、
    TLS の扱い、``X-Access-Token`` による多重化可否、secure 既定の envd トークン発行・検証方式。
    ``E2B_SANDBOX_URL`` は envd のみ・``E2B_DEBUG=true`` は sandbox_id が消える制約も spike 対象。
"""

from __future__ import annotations

from enum import Enum

# --- 認証ヘッダ ----------------------------------------
HEADER_API_KEY = "X-API-KEY"  # 制御プレーン（SDK が必ず送る）。値は .cube/token（e2b_<hex32>）。
HEADER_ACCESS_TOKEN = "X-Access-Token"  # envd データプレーン（create 応答の envd トークン）。
HEADER_ISOLATION_LEVEL = "X-Isolation-Level"  # デバッグ用補助（正典は get_info の metadata）。

# --- データプレーンのポート（サブドメイン形式の {port} 部） ---------
ENVD_PORT = 49983  # envd（files / process）。Connect RPC/protobuf 面（忠実再現は spike）。
RUN_CODE_PORT = 49999  # run_code。トランスポートは固定バージョンで要確認（spike）。

# create 応答が返すサブドメイン形式（{port}-{sandbox_id}.{sandbox_domain}、既定 https）。
# TODO: sandbox_domain のローカル解決方式は spike で確定（骨子はローカル JSON API で代替）。
SUBDOMAIN_TEMPLATE = "{port}-{sandbox_id}.{sandbox_domain}"


class Plane(Enum):
    """要求が属するプレーン（認証方式が異なる）。"""

    CONTROL = "control"  # X-API-KEY 検証
    ENVD = "envd"  # X-Access-Token 検証（サンドボックス単位）


class Operation(Enum):
    """v0 でシムが実装する 5 系統の操作。"""

    SANDBOX_CREATE = "sandbox_create"  # 制御: create
    SANDBOX_DESTROY = "sandbox_destroy"  # 制御: destroy
    SANDBOX_INFO = "sandbox_info"  # 制御: get_info（metadata に isolation_level）
    RUN_CODE = "run_code"  # envd: run_code（49999）
    FILE_WRITE = "file_write"  # envd: files write（49983）
    FILE_READ = "file_read"  # envd: files read（49983）


# 各操作が属するプレーン（認証ヘッダの選択に使う）。
OPERATION_PLANE: dict[Operation, Plane] = {
    Operation.SANDBOX_CREATE: Plane.CONTROL,
    Operation.SANDBOX_DESTROY: Plane.CONTROL,
    Operation.SANDBOX_INFO: Plane.CONTROL,
    Operation.RUN_CODE: Plane.ENVD,
    Operation.FILE_WRITE: Plane.ENVD,
    Operation.FILE_READ: Plane.ENVD,
}

# プレーン → 検証すべき認証ヘッダ名。
PLANE_AUTH_HEADER: dict[Plane, str] = {
    Plane.CONTROL: HEADER_API_KEY,
    Plane.ENVD: HEADER_ACCESS_TOKEN,
}


def auth_header_for(op: Operation) -> str:
    """操作に対応する認証ヘッダ名を返す。"""
    return PLANE_AUTH_HEADER[OPERATION_PLANE[op]]
