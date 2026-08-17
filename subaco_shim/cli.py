"""subaco-shim の CLI エントリポイント（``subaco-shim = subaco_shim.cli:main``）。

サブコマンド:

- ``subaco-shim serve``  : ローカル HTTP シムを起動する（``cube-shim`` ランチャが呼ぶ）。
  単一インスタンス flock / 永続トークン / 127.0.0.1 bind / 動的ポート公開 / アイドル終了を
  :mod:`subaco_shim.lifecycle` 経由で行い、認証・default-deny enforce・ドライバ結線は
  :mod:`subaco_shim.server` が担う。
- ``subaco-shim status`` : 設定・.cube パス・トークン／ポート・稼働状態を表示する（任意）。

「HTTP はまず stdlib http.server で実装」に沿って、サーブ本体は
:mod:`http.server` ベース（:mod:`subaco_shim.server`）で実装する。
"""

from __future__ import annotations

import argparse
import sys

from ._version import __version__
from .config import CubePaths, EnvKeys, ShimConfig, env_str
from .drivers import build_driver
from .lifecycle import DEFAULT_IDLE_TIMEOUT, serve
from .logging import get_logger
from .tokens import (
    SingleInstanceError,
    acquire_single_instance,
    read_port,
    read_token,
)


def _mask_token(token: str | None) -> str:
    """トークンを伏せ字化して表示する（先頭のプレフィクスと末尾数桁のみ）。"""
    if not token:
        return "(none)"
    if len(token) <= 8:
        return "e2b_****"
    return f"{token[:6]}…{token[-4:]}"


def _cmd_serve(args: argparse.Namespace) -> int:
    """シムを起動して 127.0.0.1 の動的ポートで serve する。"""
    log = get_logger("cli")
    config = ShimConfig.load()
    paths = CubePaths.resolve(args.project_root)
    paths.ensure_dir()

    # 実行バックエンドドライバを選択（auto はホスト検出、契約テスト/dev は --driver mock）。
    try:
        driver = build_driver(args.driver)
    except (ValueError, RuntimeError) as exc:
        log.error("driver_selection_failed detail=%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # 接続時の既定テンプレート（.envrc の CUBE_TEMPLATE_ID。create 要求に無ければ使う）。
    default_template = env_str(EnvKeys.CUBE_TEMPLATE_ID)

    try:
        serve(
            paths=paths,
            config=config,
            driver=driver,
            idle_timeout=args.idle_timeout,
            host=args.host,
            default_template_id=default_template,
        )
    except SingleInstanceError as exc:
        # 既に稼働中（単一インスタンス）。オンデマンド起動の二重起動は正常系。
        log.info("startup_skipped reason=already-running detail=%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.info("shim_interrupted")
        return 0
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """設定・.cube 状態・稼働状態を表示する。"""
    config = ShimConfig.load()
    paths = CubePaths.resolve(args.project_root)

    # 稼働状態の推定: flock を取得できれば非稼働（取得後すぐ解放）、失敗なら稼働中。
    running = False
    if paths.writer_lock.exists():
        try:
            acquire_single_instance(paths).release()
        except SingleInstanceError:
            running = True

    lines = [
        f"subaco-shim {__version__}",
        f"config_path: {config.source_path or '(defaults; file not found)'}",
        f"allow_shared_kernel: {config.allow_shared_kernel}",
        f"registered_remotes: {[r.domain for r in config.remotes] or '(none)'}",
        f"cube_dir: {paths.root}",
        f"token: {_mask_token(read_token(paths))}",
        f"port: {read_port(paths) if read_port(paths) is not None else '(none)'}",
        f"tls_cert: {paths.tls_cert if paths.tls_cert.is_file() else '(not generated)'}",
        f"ca_bundle: {paths.tls_ca_bundle if paths.tls_ca_bundle.is_file() else '(not generated)'}",
        f"running: {running}",
        f"env.{EnvKeys.E2B_DOMAIN}: {env_str(EnvKeys.E2B_DOMAIN) or '(unset)'}",
        f"env.{EnvKeys.CUBE_TEMPLATE_ID}: {env_str(EnvKeys.CUBE_TEMPLATE_ID) or '(unset)'}",
    ]
    print("\n".join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """argparse パーサを構築する。"""
    parser = argparse.ArgumentParser(
        prog="subaco-shim",
        description="E2B API 互換ローカル実行シム（cube-shim）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"subaco-shim {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="ローカル HTTP シムを起動する")
    serve_p.add_argument(
        "--project-root",
        default=None,
        help=".cube を配置するプロジェクトルート（既定は CWD）",
    )
    serve_p.add_argument(
        "--driver",
        default="auto",
        choices=["auto", "podman", "mock", "container", "wslc"],
        help="実行バックエンド（auto=ホスト検出。契約テスト/dev は mock）",
    )
    serve_p.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="無操作でシムを自動終了する秒数（0 以下で常駐）",
    )
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind ホスト（ループバック限定。既定 127.0.0.1）",
    )
    serve_p.set_defaults(func=_cmd_serve)

    status_p = sub.add_parser("status", help="設定・.cube 状態・稼働状態を表示する")
    status_p.add_argument("--project-root", default=None)
    status_p.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """コンソールスクリプトのエントリポイント。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
