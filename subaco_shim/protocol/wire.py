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

**ワイヤの実体は E2B ワイヤ spike で実測確定済み**（docs/00-memo/05_spike結果_E2B_ワイヤ.md・
判定 full-fidelity-feasible・成果物 spikes/e2b-wire/。
固定 e2b==2.30.0 / e2b-code-interpreter==2.8.1）:
  - envd（49983）は素の HTTP 3 本（``GET /files``・``POST /files``〔multipart〕・
    ``GET /health``）で足りる。**Connect RPC/protobuf 面は v0 の 5 系統では呼ばれない**
    （拡張時も SDK は JSON codec 固定）。
  - run_code（49999）は ``POST /execute`` の chunked HTTP/1.1・改行区切り JSON ストリーム
    （SSE/WS ではない。空行禁止・timestamp ns 必須・終端はボディ終端）。
  - サブドメイン解決は「create 応答 domain へのポート埋め込み（``sbx.localhost:{TLS ポート}``）＋
    ``*.sbx.localhost`` 3 ラベルワイルドカード自己署名証明書＋ ``SSL_CERT_FILE``」の単一 TLS
    リスナー（ALPN h2 非広告）。多重化は Host ヘッダの ``{port}-{sandbox_id}`` 等で成立。
    ``E2B_DEBUG`` は create/kill が HTTP に出ないため不採用、制御プレーンは ``E2B_API_URL`` の平文。

実装タスク（M2a——spike レポート §6 の差分作業 1〜7）で本モジュールを実配線化する。
"""

from __future__ import annotations

from enum import Enum

# --- 認証ヘッダ ----------------------------------------
HEADER_API_KEY = "X-API-KEY"  # 制御プレーン（SDK が必ず送る）。値は .cube/token（e2b_<hex32>）。
HEADER_ACCESS_TOKEN = "X-Access-Token"  # envd データプレーン（create 応答の envd トークン）。
HEADER_ISOLATION_LEVEL = "X-Isolation-Level"  # デバッグ用補助（正典は get_info の metadata）。

# --- データプレーンのポート（サブドメイン形式の {port} 部） ---------
ENVD_PORT = 49983  # envd（files / health）。v0 は素の HTTP 3 本で足りる（spike 確定）。
RUN_CODE_PORT = 49999  # run_code（POST /execute、chunked JSON lines——spike 確定）。

# create 応答が返すサブドメイン形式（{port}-{sandbox_id}.{sandbox_domain}、既定 https）。
# ローカル解決は「domain へのポート埋め込み + *.sbx.localhost 単一 TLS リスナー」で確定
# （05_spike結果 §2。実配線は M2a の実装タスク）。
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
