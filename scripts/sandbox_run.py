#!/usr/bin/env python3
"""sandbox_run.py — 隔離環境でエージェント生成コードを検証する（M2a-4 本実装）。

============================================================================
このファイルは **リファレンス（同梱コピー）** です。
**正典（canonical）はテンプレート側（subaco の multi-agent テンプレート）** に置かれ、
プロジェクトへ scaffold されます（テンプレート組み込みは M2b-3）。shim リポジトリには、
SDK 契約・構造化出力形状の参照実装として同梱しています。
============================================================================

役割（協業ループ）:

    エージェントA がコード生成 → sandbox_run で検証 → 結果を「隔離レベル込み」で
    hive_remember(kind=finding) に記録 → エージェントB が hive_recall で参照。

このスクリプトは **検証結果と隔離レベルを構造化 JSON で返すだけ**で、hive への記録は
呼び出し元エージェントが hive_remember で行う（単一ライター境界）。

単一契約: 接続先が cube-shim / CubeSandbox / ホステッド E2B のいずれでも
本コードは無改変で動く。接続先切替は `.envrc` の接続設定で行う
（cube-shim は `E2B_API_URL` + `SSL_CERT_FILE`——spike 確定構成）。

- **リトライ**: サンドボックス作成の接続確立失敗のみ再試行する（シムのオンデマンド起動
  直後の race・アイドル終了からの再起動待ち）。実行済みコードの再送はしない
  （/execute の再送は SDK 側でも起きない——spike §1.3。二重実行の副作用を避ける）。
- **タイムアウト**: 実行タイムアウトは run_code に渡す。SDK はチャンク間無通信時間
  （read タイムアウト）として扱う（spike §1.3）。既定は SDK の 300 秒。
- **隔離レベル**: get_info の metadata（正典経路）から取得。無ければ **unknown
  （fail-closed）** — 隔離保証なしとして扱う。登録済みリモートへの microvm 判定は
  シム側の責務（設計書 §5.3。クライアントは申告しない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any

# 隔離レベルキー（get_info の metadata に載る値）。
ISOLATION_LEVEL_KEY = "isolation_level"
# fail-closed の既定（metadata に無い＝隔離保証なしとして扱う）。
UNKNOWN_ISOLATION = "unknown"

# サンドボックス作成の再試行既定（回数と初回待機秒。待機は指数バックオフ）。
DEFAULT_CREATE_RETRIES = 3
DEFAULT_RETRY_WAIT = 0.2


def _default_sandbox_factory() -> Callable[..., Any]:
    """e2b_code_interpreter.Sandbox を **遅延 import** で返す（未導入でも本 module は import 可）。

    外部依存（e2b SDK）が import できなくてもモジュール自体は壊れない。
    """
    from e2b_code_interpreter import Sandbox  # type: ignore[import-not-found]

    return Sandbox.create


def _is_retryable_create_error(exc: Exception) -> bool:
    """作成時の例外が接続確立の失敗（再試行可能）か。

    認証エラー・API エラー等の非一時的失敗は再試行しない。httpx は e2b SDK の依存として
    存在する前提だが、遅延 import で不在でも壊れない。
    """
    if isinstance(exc, ConnectionError | TimeoutError | OSError):
        return True
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        return False
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.ReadTimeout)


def _extract_isolation_level(info: Any) -> str:
    """get_info 返却から隔離レベル文字列を取り出す（無ければ unknown — fail-closed）。

    E2B SDK の get_info 返却（SandboxInfo）は metadata 属性を持つ。接続先・バージョン
    差異に備えて dict 形も受ける。
    """
    metadata = getattr(info, "metadata", None)
    if metadata is None and isinstance(info, dict):
        metadata = info.get("metadata")
    if isinstance(metadata, dict):
        level = metadata.get(ISOLATION_LEVEL_KEY)
        if level:
            return str(level)
    return UNKNOWN_ISOLATION


def _execution_to_dict(execution: Any) -> dict[str, Any] | None:
    """Execution を JSON 化可能な dict へ（実 e2b SDK は to_json のみ、骨子は to_dict を持つ）。"""
    to_dict = getattr(execution, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    to_json = getattr(execution, "to_json", None)
    if callable(to_json):
        try:
            parsed = json.loads(to_json())
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _create_with_retry(
    factory: Callable[..., Any],
    *,
    template: str | None,
    retries: int,
    retry_wait: float,
) -> Any:
    """サンドボックスを作成する。接続確立の失敗のみ指数バックオフで再試行する。"""
    attempt = 0
    while True:
        try:
            return factory(template=template)
        except Exception as exc:
            if attempt >= retries or not _is_retryable_create_error(exc):
                raise
            time.sleep(retry_wait * (2**attempt))
            attempt += 1


def run_untrusted(
    code: str,
    *,
    template_id: str | None = None,
    sandbox_factory: Callable[..., Any] | None = None,
    timeout: float | None = None,
    retries: int = DEFAULT_CREATE_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT,
) -> dict[str, Any]:
    """コードを隔離環境で実行し、hive_remember に渡せる構造化 dict を返す。

    引数:
        code: 実行するコード（エージェント生成の未信頼コード）。
        template_id: OCI テンプレート参照（既定は環境変数 ``CUBE_TEMPLATE_ID``）。
        sandbox_factory: ``Sandbox.create`` 相当（テスト時に差替可。既定は e2b を遅延 import）。
        timeout: 実行タイムアウト秒（run_code に渡す。None は SDK 既定 300 秒）。
        retries: 作成時の接続失敗の最大再試行回数（実行の再送はしない）。
        retry_wait: 再試行の初回待機秒（指数バックオフ）。

    返り値（構造化出力）:
        ``ok`` / ``text`` / ``isolation_level`` / ``template_id`` / ``execution`` / ``error``。
    """
    template = template_id or os.environ.get("CUBE_TEMPLATE_ID")
    factory = sandbox_factory or _default_sandbox_factory()

    result: dict[str, Any] = {
        "ok": False,
        "text": None,
        ISOLATION_LEVEL_KEY: UNKNOWN_ISOLATION,
        "template_id": template,
        "execution": None,
        "error": None,
    }

    try:
        with _create_with_retry(
            factory, template=template, retries=retries, retry_wait=retry_wait
        ) as sb:
            # timeout=None は kwargs ごと省略し SDK 既定（300 秒）に委ねる。
            run_kwargs = {} if timeout is None else {"timeout": timeout}
            execution = sb.run_code(code, **run_kwargs)
            # run_code は Execution を返す。出力は .text、構造化は .to_dict()/.to_json()。
            # str(Execution) は repr を返すため使わない。
            result["text"] = getattr(execution, "text", None)
            result["execution"] = _execution_to_dict(execution)
            # 隔離レベルは get_info の metadata から取得（正典な返却経路）。
            info = sb.get_info()
            result[ISOLATION_LEVEL_KEY] = _extract_isolation_level(info)
            result["ok"] = getattr(execution, "error", None) is None
    except Exception as exc:  # noqa: BLE001 - 呼び出し元へ構造化エラーで返す
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    """コードを stdin（または引数）で受け取り、構造化 JSON を stdout に出力する。"""
    parser = argparse.ArgumentParser(
        prog="sandbox_run",
        description="隔離環境でコードを検証し、構造化 JSON（隔離レベル込み）を出力する",
    )
    parser.add_argument("code", nargs="?", default=None, help="実行コード（省略時は stdin）")
    parser.add_argument(
        "--template",
        default=None,
        help="OCI テンプレート参照（既定は環境変数 CUBE_TEMPLATE_ID）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="実行タイムアウト秒（既定は SDK の 300 秒）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_CREATE_RETRIES,
        help="作成時の接続失敗の最大再試行回数",
    )
    args = parser.parse_args(argv)

    code = args.code if args.code is not None else sys.stdin.read()
    payload = run_untrusted(
        code, template_id=args.template, timeout=args.timeout, retries=args.retries
    )
    # hive_remember へそのまま渡せる構造化 JSON（記録は呼び出し元が行う）。
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
