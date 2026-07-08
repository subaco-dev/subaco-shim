"""cli.py のエントリポイント・サブコマンドの smoke テスト。"""

from __future__ import annotations

import pytest

from subaco_shim import __version__
from subaco_shim.cli import build_parser, main
from subaco_shim.config import CubePaths
from subaco_shim.tokens import read_token


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_no_command_prints_help(capsys):
    rc = main([])
    assert rc == 2  # サブコマンド未指定は usage を出して 2 を返す。


def test_status_runs(tmp_path, capsys):
    rc = main(["status", "--project-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "subaco-shim" in out
    assert "allow_shared_kernel" in out
    assert "cube_dir" in out


def test_serve_starts_and_idle_exits(tmp_path):
    # 段階 2: serve は実際に起動し、アイドルタイムアウトで自動終了する。
    # 契約テスト/dev では --driver mock を使う（このマシンに podman は無い）。
    rc = main(
        ["serve", "--project-root", str(tmp_path), "--driver", "mock", "--idle-timeout", "0.2"]
    )
    assert rc == 0
    # 起動時にトークン（0600 永続）とポートが公開されていること。
    paths = CubePaths.resolve(tmp_path)
    assert read_token(paths) is not None
    from subaco_shim.tokens import read_port

    assert read_port(paths) is not None


def test_serve_second_instance_rejected(tmp_path):
    from subaco_shim.tokens import acquire_single_instance

    paths = CubePaths.resolve(tmp_path)
    lock = acquire_single_instance(paths)
    try:
        rc = main(["serve", "--project-root", str(tmp_path), "--driver", "mock"])
        assert rc == 1  # 既に稼働中なら 1 を返す。
    finally:
        lock.release()


def test_serve_auto_without_backend_fails(tmp_path, monkeypatch):
    # auto でバックエンド不在なら driver 選択失敗で 1 を返す（ホスト非依存に模擬）。
    for cls in ("AppleContainerDriver", "PodmanDriver", "WslcDriver"):
        monkeypatch.setattr(f"subaco_shim.drivers.{cls}.available", classmethod(lambda c: False))
    rc = main(["serve", "--project-root", str(tmp_path), "--driver", "auto"])
    assert rc == 1


def test_parser_builds():
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"
