"""config.py の設定読取・.cube レイアウト・fail-closed 解決のテスト。"""

from __future__ import annotations

import stat

from subaco_shim.config import CubePaths, ShimConfig
from subaco_shim.isolation import IsolationLevel


def test_defaults_are_fail_closed(tmp_path):
    # config.toml 不在時は allow_shared_kernel=false・登録リモートなし（安全側既定）。
    cfg = ShimConfig.load(tmp_path / "does-not-exist.toml")
    assert cfg.allow_shared_kernel is False
    assert cfg.remotes == ()
    assert cfg.source_path is None


def test_load_allow_shared_kernel_and_remotes(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "allow_shared_kernel = true\n"
        "[[remote]]\n"
        'domain = "cube.example.com"\n'
        'kind = "cubesandbox"\n'
        "[[remote]]\n"
        'domain = "e2b.example.com"\n'
        'kind = "hosted-e2b"\n',
        encoding="utf-8",
    )
    cfg = ShimConfig.load(p)
    assert cfg.allow_shared_kernel is True
    assert {r.domain for r in cfg.remotes} == {"cube.example.com", "e2b.example.com"}
    assert cfg.source_path == p


def test_trusted_remotes_simple_list(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('trusted_remotes = ["a.example.com"]\n', encoding="utf-8")
    cfg = ShimConfig.load(p)
    assert cfg.is_registered_remote("a.example.com") is True
    assert cfg.is_registered_remote("b.example.com") is False


def test_isolation_level_for_remote_fail_closed(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[[remote]]\ndomain = "reg.example.com"\n', encoding="utf-8")
    cfg = ShimConfig.load(p)
    # 登録済みのみ microvm、未登録・None は unknown。
    assert (
        cfg.isolation_level_for_remote("reg.example.com") is IsolationLevel.MICROVM_DEDICATED_KERNEL
    )
    assert cfg.isolation_level_for_remote("other.example.com") is IsolationLevel.UNKNOWN
    assert cfg.isolation_level_for_remote(None) is IsolationLevel.UNKNOWN


def test_cube_paths_layout(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    assert paths.root == tmp_path / ".cube"
    assert paths.port == tmp_path / ".cube" / "port"
    assert paths.token == tmp_path / ".cube" / "token"
    assert paths.writer_lock == tmp_path / ".cube" / "writer.lock"


def test_cube_ensure_dir_is_0700(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    paths.ensure_dir()
    mode = stat.S_IMODE(paths.root.stat().st_mode)
    assert mode == 0o700
    # 冪等（再呼び出しでも 0700 を維持）。
    paths.ensure_dir()
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
