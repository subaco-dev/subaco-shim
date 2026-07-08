"""診断ログ設定。

cube-shim はローカル HTTP サーバーであり、hive-mcp（stdout を JSON-RPC 専有）と
異なり **stdout / stderr / プロジェクトローカルのログファイル** を出力先に使える。
レベルは環境変数 ``SUBACO_SHIM_LOG_LEVEL``（既定 info）で
制御する。ログのキー・メッセージは英語。

必須ログ点（呼び出し側が使うためのヘルパを提供）:
- オンデマンド起動 / アイドル終了
- ポート／トークンファイル解決
- ドライバ呼び出し失敗
- egress 遮断下のデータプレーン（envd / run_code）接続失敗

stdlib ``logging`` のみを使う（外部依存なし）。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .config import EnvKeys

_LOGGER_ROOT = "subaco_shim"
_DEFAULT_LEVEL = "info"

# 環境変数値（大小文字非依存）→ logging レベルの対応。
_LEVELS: dict[str, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 二重ハンドラ付与を避けるためのフラグ。
_configured = False


def resolve_level(value: str | None = None) -> int:
    """``SUBACO_SHIM_LOG_LEVEL`` の値（または引数）を logging レベルへ解決する。

    未設定・未知の値は既定 info。
    """
    raw = value if value is not None else os.environ.get(EnvKeys.SUBACO_SHIM_LOG_LEVEL)
    key = (raw or _DEFAULT_LEVEL).strip().lower()
    return _LEVELS.get(key, logging.INFO)


def configure_logging(
    *,
    level: str | int | None = None,
    log_file: Path | str | None = None,
    stream: object | None = None,
) -> logging.Logger:
    """``subaco_shim`` ルートロガーを設定して返す（冪等）。

    引数:
        level: 明示レベル（int / 名前）。None なら環境変数から解決。
        log_file: 指定時はプロジェクトローカルのログファイルにも出力する
            （例: ``.cube/`` 配下。呼び出し側が 0700 を保証すること）。
        stream: 出力ストリーム（既定 stderr。stdout も可 — hive と違い JSON-RPC 制約なし）。

    既定の出力先は stderr。stdout を JSON 応答等に使う経路と衝突しないための保守的既定。
    """
    global _configured
    logger = logging.getLogger(_LOGGER_ROOT)
    resolved = level if isinstance(level, int) else resolve_level(level)
    logger.setLevel(resolved)

    if not _configured:
        formatter = logging.Formatter(_LOG_FORMAT)
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if log_file is not None:
            file_handler = logging.FileHandler(Path(log_file), encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        # ルートロガーへの伝播を止め、二重出力を避ける。
        logger.propagate = False
        _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """名前付き子ロガーを返す（``subaco_shim`` 名前空間配下）。

    ``configure_logging`` 未呼び出しでも安全に使えるよう、必要なら既定設定を行う。
    """
    if not _configured:
        configure_logging()
    if not name:
        return logging.getLogger(_LOGGER_ROOT)
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")
