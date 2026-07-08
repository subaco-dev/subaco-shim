"""tokens.py のトークン永続・再利用・ポート・単一インスタンス flock のテスト。"""

from __future__ import annotations

import stat

import pytest

from subaco_shim.config import CubePaths
from subaco_shim.tokens import (
    SingleInstanceError,
    acquire_single_instance,
    generate_token,
    is_valid_token_format,
    load_or_create_token,
    read_port,
    read_token,
    write_port,
)


def test_token_format():
    tok = generate_token()
    assert is_valid_token_format(tok)
    assert tok.startswith("e2b_")
    assert len(tok) == len("e2b_") + 32
    assert not is_valid_token_format("nope")
    assert not is_valid_token_format("e2b_short")
    assert not is_valid_token_format(None)


def test_token_persisted_0600_and_reused(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    first = load_or_create_token(paths)
    # 0600 で永続していること。
    mode = stat.S_IMODE(paths.token.stat().st_mode)
    assert mode == 0o600
    # 再起動相当の再呼び出しで同じトークンを再利用すること（毎回再生成しない）。
    second = load_or_create_token(paths)
    assert first == second
    assert read_token(paths) == first


def test_token_regenerated_when_malformed(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    paths.ensure_dir()
    paths.token.write_text("garbage", encoding="ascii")
    tok = load_or_create_token(paths)
    assert is_valid_token_format(tok)
    assert read_token(paths) == tok


def test_port_roundtrip(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    assert read_port(paths) is None
    write_port(paths, 49213)
    assert read_port(paths) == 49213


def test_single_instance_lock_excludes_second(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    lock = acquire_single_instance(paths)
    try:
        # 同一プロセス内でも同じロックファイルへの二重取得は失敗する。
        with pytest.raises(SingleInstanceError):
            acquire_single_instance(paths)
    finally:
        lock.release()
    # 解放後は再取得できる。
    lock2 = acquire_single_instance(paths)
    lock2.release()
    # ロックファイルは恒久（unlink しない）。
    assert paths.writer_lock.exists()


def test_single_instance_context_manager(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    with acquire_single_instance(paths), pytest.raises(SingleInstanceError):
        acquire_single_instance(paths)
    # with を抜ければ解放済み。
    acquire_single_instance(paths).release()
