#!/usr/bin/env python3
"""sandbox_run.py — 隔離環境でエージェント生成コードを検証する骨子。

============================================================================
このファイルは **リファレンス（同梱コピー）** です。
**正典（canonical）はテンプレート側（subaco の multi-agent テンプレート）** に置かれ、
プロジェクトへ scaffold されます。shim リポジトリには、SDK 契約・構造化出力形状の
参照実装として同梱しています。実運用のスクリプトはテンプレート側を編集してください。
============================================================================

役割（協業ループ）:

    エージェントA がコード生成 → sandbox_run で検証 → 結果を「隔離レベル込み」で
    hive_remember(kind=finding) に記録 → エージェントB が hive_recall で参照。

このスクリプトは **検証結果と隔離レベルを構造化 JSON で返すだけ**で、hive への記録は
呼び出し元エージェントが hive_remember で行う（単一ライター境界）。

単一契約: 接続先が cube-shim / CubeSandbox / ホステッド E2B のいずれでも
本コードは無改変で動く。接続先切替は `.envrc` の接続設定（`E2B_DOMAIN` 等）で行う。

TODO: リトライ・タイムアウトの実値と方針は spike 確定の接続仕様に合わせる。
TODO: 非 shim 接続先の隔離レベルは fail-closed（登録済みのみ microvm、未登録は unknown）。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

# 隔離レベルキー（get_info の metadata に載る値）。
ISOLATION_LEVEL_KEY = "isolation_level"
# fail-closed の既定（metadata に無い＝隔離保証なしとして扱う）。
UNKNOWN_ISOLATION = "unknown"


def _default_sandbox_factory() -> Callable[..., Any]:
    """e2b_code_interpreter.Sandbox を **遅延 import** で返す（未導入でも本 module は import 可）。

    外部依存（e2b SDK）が import できなくてもモジュール自体は壊れない。
    """
    from e2b_code_interpreter import Sandbox  # type: ignore[import-not-found]

    return Sandbox.create


def _extract_isolation_level(info: Any) -> str:
    """get_info 返却から隔離レベル文字列を取り出す（無ければ unknown — fail-closed）。

    E2B SDK の get_info 返却形状は接続先・バージョンで異なり得るため、metadata 相当を
    寛容に探索する。TODO: 実際の返却型に合わせて確定する。
    """
    metadata = getattr(info, "metadata", None)
    if metadata is None and isinstance(info, dict):
        metadata = info.get("metadata")
    if isinstance(metadata, dict):
        level = metadata.get(ISOLATION_LEVEL_KEY)
        if level:
            return str(level)
    return UNKNOWN_ISOLATION


def run_untrusted(
    code: str,
    *,
    template_id: str | None = None,
    sandbox_factory: Callable[..., Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """コードを隔離環境で実行し、hive_remember に渡せる構造化 dict を返す。

    引数:
        code: 実行するコード（エージェント生成の未信頼コード）。
        template_id: OCI テンプレート参照（既定は環境変数 ``CUBE_TEMPLATE_ID``）。
        sandbox_factory: ``Sandbox.create`` 相当（テスト時に差替可。既定は e2b を遅延 import）。
        timeout: 実行タイムアウト（TODO: 接続仕様に合わせて反映）。

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

    # TODO: 接続失敗時のリトライループ（egress 遮断下のデータプレーン到達確認を含む）。
    try:
        with factory(template=template) as sb:
            execution = sb.run_code(code)
            # run_code は Execution を返す。出力は .text、構造化は .to_json()。
            # str(Execution) は repr を返すため使わない。
            result["text"] = getattr(execution, "text", None)
            to_dict = getattr(execution, "to_dict", None)
            if callable(to_dict):
                result["execution"] = to_dict()
            # 隔離レベルは get_info の metadata から取得（正典な返却経路）。
            info = sb.get_info()
            result[ISOLATION_LEVEL_KEY] = _extract_isolation_level(info)
            result["ok"] = getattr(execution, "error", None) is None
    except Exception as exc:  # noqa: BLE001 - 呼び出し元へ構造化エラーで返す
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    """コードを stdin（または引数）で受け取り、構造化 JSON を stdout に出力する。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    code = argv[0] if argv else sys.stdin.read()
    payload = run_untrusted(code)
    # hive_remember へそのまま渡せる構造化 JSON（記録は呼び出し元が行う）。
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
