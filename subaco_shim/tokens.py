""".cube/token の生成・永続・再利用、.cube/port と単一インスタンス flock。

- **トークン**（``.cube/token``）: SDK のキー形式を満たす ``e2b_<hex32>``。
  プロジェクトごとに**初回のみ生成**して 0600 で永続し、（再）起動時は既存ファイルを
  読み込んで**再利用する**（毎起動での再生成はしない — アイドル終了・再起動をまたいで
  既存セッションの認証を維持するため）。
- **ポート**（``.cube/port``）: シムの listen ポート。動的割当（0 番指定で OS が付与）した
  実ポートを書き出し、`.envrc` / sandbox_run.py は呼び出し時に読み直す。
- **単一インスタンス**: ``.cube/writer.lock`` に対する ``flock``（LOCK_EX|LOCK_NB）で
  プロジェクト単位に 1 プロセスを保証する。ロックファイルは**恒久**で稼働中に unlink しない
  （`.hive/` の writer.lock と同型）。

``fcntl`` を用いるため Unix / WSL2 前提（ネイティブ Windows 非対応）。
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from .config import CUBE_TOKEN_MODE, CubePaths

# SDK のキー形式（e2b_<hex32>）。hex32 = 16 バイト = 32 桁の 16 進。
_TOKEN_HEX_BYTES = 16
_TOKEN_RE = re.compile(r"^e2b_[0-9a-f]{32}$")


def generate_token() -> str:
    """``e2b_<hex32>`` 形式の乱数トークンを生成する。"""
    return "e2b_" + secrets.token_hex(_TOKEN_HEX_BYTES)


def is_valid_token_format(token: str | None) -> bool:
    """トークンが ``e2b_<hex32>`` 形式かを検証する（X-API-KEY 検証で使用）。"""
    return bool(token) and bool(_TOKEN_RE.match(token or ""))


def _write_secret(path: Path, content: str) -> None:
    """0600 でファイルを作成し content を書く（umask の影響を受けないよう明示 chmod）。"""
    # O_CREAT 時のモード指定 + 既存ファイルへの chmod の二段で 0600 を保証する。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CUBE_TOKEN_MODE)
    try:
        os.write(fd, content.encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(path, CUBE_TOKEN_MODE)


def load_or_create_token(paths: CubePaths) -> str:
    """既存 ``.cube/token`` を再利用、無ければ生成して 0600 で永続する。

    既存ファイルが不正形式（手動編集・破損）の場合のみ再生成する。
    """
    paths.ensure_dir()
    if paths.token.is_file():
        existing = paths.token.read_text(encoding="ascii", errors="replace").strip()
        if is_valid_token_format(existing):
            # 権限が緩んでいる可能性に備え 0600 へ締め直す。
            os.chmod(paths.token, CUBE_TOKEN_MODE)
            return existing
        # 不正形式は再生成する（TODO: logging.py 経由で warning を出す）。
    token = generate_token()
    _write_secret(paths.token, token)
    return token


def read_token(paths: CubePaths) -> str | None:
    """``.cube/token`` を読む（不在・不正形式は None）。"""
    if not paths.token.is_file():
        return None
    tok = paths.token.read_text(encoding="ascii", errors="replace").strip()
    return tok if is_valid_token_format(tok) else None


def write_port(paths: CubePaths, port: int) -> None:
    """実 listen ポートを ``.cube/port`` に書く（秘密ではないため 0644 で可）。"""
    paths.ensure_dir()
    paths.port.write_text(f"{int(port)}\n", encoding="ascii")


def read_port(paths: CubePaths) -> int | None:
    """``.cube/port`` を読む（不在・不正値は None）。"""
    if not paths.port.is_file():
        return None
    raw = paths.port.read_text(encoding="ascii", errors="replace").strip()
    try:
        return int(raw)
    except ValueError:
        return None


class SingleInstanceError(RuntimeError):
    """既に同一プロジェクトのシムが稼働中で flock を取得できない場合に送出。"""


@dataclass
class InstanceLock:
    """``.cube/writer.lock`` の flock 保持を表すハンドル。

    :meth:`release` まで（またはプロセス終了まで）ロックを保持する。ロックファイル
    自体は unlink しない（恒久ロックファイル）。
    """

    fd: int
    path: Path
    _released: bool = False

    def release(self) -> None:
        """flock を解放し fd を閉じる（ファイルは削除しない）。冪等。"""
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire_single_instance(paths: CubePaths) -> InstanceLock:
    """``.cube/writer.lock`` に排他 flock を取得し単一インスタンスを保証する。

    既に別プロセスが保持していれば :class:`SingleInstanceError` を送出する。
    ロックは返り値の :class:`InstanceLock` が保持し、release / プロセス終了で解放される。
    """
    paths.ensure_dir()
    # 恒久ロックファイルを開く（存在すれば再利用。unlink+flock レースを避けるため削除しない）。
    fd = os.open(paths.writer_lock, os.O_RDWR | os.O_CREAT, CUBE_TOKEN_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise SingleInstanceError(
            f"別のシムインスタンスが稼働中です（lock={paths.writer_lock}）"
        ) from exc
    return InstanceLock(fd=fd, path=paths.writer_lock)
