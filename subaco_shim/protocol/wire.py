"""E2B ワイヤプロトコルの面（プレーン・オペレーション・ヘッダ・ポート・サブドメイン解決）。

**実配線仕様は E2B ワイヤ spike で実測確定済み**（docs/00-memo/05_spike結果_E2B_ワイヤ.md・
判定 full-fidelity-feasible・成果物 spikes/e2b-wire/。
固定 e2b==2.30.0 / e2b-code-interpreter==2.8.1）:

- **制御プレーン**（``E2B_API_URL`` の平文 HTTP・認証 ``X-API-KEY``）:
  ``POST /sandboxes``(201)・``GET /sandboxes/{id}``(200)・``DELETE /sandboxes/{id}``(204/404)。
  エラーボディは ``{"code": int, "message": str}``。
- **envd データプレーン**（ポート 49983 面・認証 ``X-Access-Token``）: 素の HTTP 3 本
  （``GET /files``・``POST /files``〔multipart〕・``GET /health``）。Connect RPC/protobuf 面は
  v0 の 5 系統では呼ばれない（拡張時も SDK は JSON codec 固定）。
- **run_code データプレーン**（ポート 49999 面・認証 ``X-Access-Token``）:
  ``POST /execute`` の chunked HTTP/1.1・改行区切り JSON ストリーム
  （SSE/WS ではない。空行禁止・stdout/stderr の timestamp ns 必須・終端はボディ終端）。
- **サブドメイン解決**: create 応答 domain へのポート埋め込み
  （``sbx.localhost:{TLS ポート}``）＋ ``*.sbx.localhost`` 3 ラベルワイルドカード自己署名
  証明書＋ ``SSL_CERT_FILE`` の単一 TLS リスナー（ALPN h2 非広告）。多重化は Host ヘッダの
  ``{port}-{sandbox_id}`` で成立。``E2B_DEBUG`` は create/kill が HTTP に出ないため不採用。
"""

from __future__ import annotations

from enum import Enum

# --- 認証ヘッダ ----------------------------------------
HEADER_API_KEY = "X-API-KEY"  # 制御プレーン（SDK が必ず送る）。値は .cube/token（e2b_<hex32>）。
HEADER_ACCESS_TOKEN = "X-Access-Token"  # データプレーン（create 応答の envd トークン）。
HEADER_ISOLATION_LEVEL = "X-Isolation-Level"  # デバッグ用補助（正典は get_info の metadata）。
HEADER_SANDBOX_ID = "E2b-Sandbox-Id"  # envd 面に常時付与される（多重化の補助。正典は Host）。

# --- データプレーンのポート（サブドメイン形式の {port} 部） ---------
ENVD_PORT = 49983  # envd（files / health）。素の HTTP 3 本で足りる（spike 確定）。
RUN_CODE_PORT = 49999  # run_code（POST /execute、chunked JSON lines——spike 確定）。

# SDK が名乗らせる envd バージョン。"0.5.5" は username 省略が既定・/files が multipart のみで
# 完結する最小（octet-stream 形態は >=0.5.7 で有効化——spike §1.2 裁定）。
ENVD_VERSION = "0.5.5"

# create 応答 domain のベース。OpenSSL は 2 ラベルワイルドカード `*.localhost` を拒否するため
# 3 ラベル `*.sbx.localhost` を用いる（spike §2(e)）。実際の domain はポート埋め込み形
# `sbx.localhost:{TLS ポート}`（build_domain）。
SANDBOX_DOMAIN_BASE = "sbx.localhost"

# create 応答の clientID（SDK 必須キーだがローカルシムでは意味を持たないダミー値）。
CLIENT_ID = "cube-local"

# create 応答が返すサブドメイン形式（{port}-{sandbox_id}.{sandbox_domain}、既定 https）。
SUBDOMAIN_TEMPLATE = "{port}-{sandbox_id}.{sandbox_domain}"


def build_domain(tls_port: int) -> str:
    """create 応答の ``domain``（ポート埋め込み形）を組む。

    SDK の ``get_host`` は純粋な f-string ``{port}-{sandbox_id}.{domain}`` のため、
    ここにポートを埋め込むと全データプレーン URL が
    ``https://{port}-{id}.sbx.localhost:{tls_port}`` になり単一 TLS リスナーへ集約される。
    **domain は必ず返す**（未返却は e2b.app へフォールバックする——spike §6-5）。
    """
    return f"{SANDBOX_DOMAIN_BASE}:{int(tls_port)}"


def parse_subdomain_host(host: str | None) -> tuple[int, str] | None:
    """データプレーン要求の Host ヘッダから ``(port, sandbox_id)`` を取り出す。

    Host は ``{port}-{sandbox_id}.sbx.localhost[:{tls_port}]``（spike 実測:
    ``Host: 49999-sbx-tls-0001.sbx.localhost:8443``）。一致しなければ None。
    """
    if not host:
        return None
    # 末尾の :{tls_port} を除去（IPv6 リテラルはサブドメイン形式に現れない）。
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    suffix = "." + SANDBOX_DOMAIN_BASE
    if not hostname.endswith(suffix):
        return None
    label = hostname[: -len(suffix)]
    port_str, sep, sandbox_id = label.partition("-")
    if not sep or not sandbox_id or not port_str.isdigit():
        return None
    return int(port_str), sandbox_id


class Plane(Enum):
    """要求が属するプレーン（認証方式が異なる）。"""

    CONTROL = "control"  # X-API-KEY 検証
    ENVD = "envd"  # X-Access-Token 検証（サンドボックス単位）


class Operation(Enum):
    """v0 でシムが実装する操作（5 系統 + /health）。"""

    SANDBOX_CREATE = "sandbox_create"  # 制御: create
    SANDBOX_DESTROY = "sandbox_destroy"  # 制御: destroy
    SANDBOX_INFO = "sandbox_info"  # 制御: get_info（metadata に isolation_level）
    RUN_CODE = "run_code"  # envd: run_code（49999 面の POST /execute）
    FILE_WRITE = "file_write"  # envd: files write（49983 面の POST /files、multipart）
    FILE_READ = "file_read"  # envd: files read（49983 面の GET /files）
    HEALTH = "health"  # envd: GET /health（run_code 接続断時に SDK が自動で叩く——実装必須）


# 各操作が属するプレーン（認証ヘッダの選択に使う）。
OPERATION_PLANE: dict[Operation, Plane] = {
    Operation.SANDBOX_CREATE: Plane.CONTROL,
    Operation.SANDBOX_DESTROY: Plane.CONTROL,
    Operation.SANDBOX_INFO: Plane.CONTROL,
    Operation.RUN_CODE: Plane.ENVD,
    Operation.FILE_WRITE: Plane.ENVD,
    Operation.FILE_READ: Plane.ENVD,
    Operation.HEALTH: Plane.ENVD,
}

# プレーン → 検証すべき認証ヘッダ名。
PLANE_AUTH_HEADER: dict[Plane, str] = {
    Plane.CONTROL: HEADER_API_KEY,
    Plane.ENVD: HEADER_ACCESS_TOKEN,
}


def auth_header_for(op: Operation) -> str:
    """操作に対応する認証ヘッダ名を返す。"""
    return PLANE_AUTH_HEADER[OPERATION_PLANE[op]]
