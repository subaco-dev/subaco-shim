"""シムのライフサイクル（オンデマンド起動・単一インスタンス・アイドル終了）。

責務:

- **オンデマンド起動**（devShell 起動スクリプト／sandbox_run.py が初回利用時に起動）。
- **単一インスタンス**: ``.cube/writer.lock`` の flock（プロジェクト単位に 1 プロセス）。
- **動的ポート**: 127.0.0.1:0 に bind し OS 付与の実ポートを ``.cube/port`` へ書く。再起動で
  ポートが変わっても、呼び出し側は ``.cube/port`` を読み直して接続を回復する。
- **永続トークン再利用**: ``.cube/token`` を初回のみ生成・0600 で永続し、（再）起動時は
  既存を読み込んで再利用（アイドル終了・再起動をまたいで既存セッションの認証を維持）。
- **アイドルタイムアウト終了**: 最終要求からの経過が閾値を超えたら自動終了する。
- **destroy 時ネットワーク掃除**: シャットダウン時に残存サンドボックスを destroy し、
  サンドボックス個別ネットワークの残骸を掃除する（ドライバ責務へ委譲）。

診断ログ点: オンデマンド起動／アイドル終了、ポート／トークンファイル解決、
ドライバ呼び出し失敗（:mod:`subaco_shim.server` 側）、egress 遮断下のデータプレーン接続失敗
（同）。``fcntl`` を用いるため Unix / WSL2 前提。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from time import monotonic

from .config import CubePaths, ShimConfig
from .drivers.base import Driver
from .logging import get_logger
from .server import ShimApp, ShimHTTPServer, make_server
from .tokens import (
    InstanceLock,
    acquire_single_instance,
    load_or_create_token,
    write_port,
)

_log = get_logger("lifecycle")

# 既定アイドルタイムアウト（秒）。0 以下で無効化（常駐）。
DEFAULT_IDLE_TIMEOUT = 300.0
# serve_forever のポーリング間隔（秒）。アイドル監視の応答性に効く。
_POLL_INTERVAL = 0.5


def _mask_token(token: str) -> str:
    """トークンをログ用に伏せ字化する（先頭プレフィクスと末尾数桁のみ）。"""
    if len(token) <= 8:
        return "e2b_****"
    return f"{token[:6]}…{token[-4:]}"


@dataclass
class RunningShim:
    """起動済みシムのハンドル（serve_forever / shutdown / close を提供）。"""

    app: ShimApp
    server: ShimHTTPServer
    port: int
    lock: InstanceLock
    paths: CubePaths
    driver: Driver
    idle_timeout: float
    _closed: bool = False
    _watchdog_stop: threading.Event | None = None

    def serve_forever(self, poll_interval: float = _POLL_INTERVAL) -> None:
        """要求を処理し続ける（アイドル監視スレッドを起動）。shutdown まで戻らない。"""
        stop = threading.Event()
        self._watchdog_stop = stop
        watchdog = threading.Thread(
            target=self._watch_idle, args=(stop, poll_interval), daemon=True
        )
        watchdog.start()
        try:
            self.server.serve_forever(poll_interval=poll_interval)
        finally:
            stop.set()
            watchdog.join(timeout=2.0)

    def _watch_idle(self, stop: threading.Event, poll: float) -> None:
        """アイドル閾値超過でサーバーを停止する。"""
        if self.idle_timeout is None or self.idle_timeout <= 0:
            return  # 無効化（常駐）。
        while not stop.wait(poll):
            idle = monotonic() - self.app.last_activity
            if idle >= self.idle_timeout:
                _log.info("shim_idle_exit idle_timeout=%s port=%s", self.idle_timeout, self.port)
                self.server.shutdown()
                return

    def shutdown(self) -> None:
        """serve_forever ループを外部から停止する（別スレッドから呼ぶこと）。"""
        self.server.shutdown()

    def close(self) -> None:
        """残存サンドボックスの掃除・サーバークローズ・flock 解放（冪等）。"""
        if self._closed:
            return
        self._closed = True
        self._cleanup_sandboxes()
        self.server.server_close()
        self.lock.release()
        _log.info("shim_stopped port=%s", self.port)

    def _cleanup_sandboxes(self) -> None:
        """生存サンドボックスを destroy し、個別ネットワーク残骸を掃除する。"""
        for sid in self.app.active_sandboxes():
            try:
                self.driver.destroy(sid)
            except Exception as exc:  # ベストエフォート（掃除は継続）。
                _log.error("cleanup_failed sandbox_id=%s error=%s", sid, type(exc).__name__)

    def __enter__(self) -> RunningShim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def start_shim(
    *,
    paths: CubePaths,
    config: ShimConfig,
    driver: Driver,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    host: str = "127.0.0.1",
    default_template_id: str | None = None,
) -> RunningShim:
    """シムを起動する（単一インスタンス確保 → トークン再利用 → 127.0.0.1 bind → ポート公開）。

    既に同一プロジェクトのシムが稼働していれば
    :class:`~subaco_shim.tokens.SingleInstanceError` を送出する（呼び出し側で捕捉）。
    返り値の :class:`RunningShim` で ``serve_forever`` / ``close`` する。
    """
    paths.ensure_dir()
    # 単一インスタンス保証（.cube/writer.lock の flock）。
    lock = acquire_single_instance(paths)
    try:
        # 永続トークンの解決（初回のみ生成、以後再利用）。
        token = load_or_create_token(paths)
        _log.info("token_resolved cube_dir=%s token=%s", paths.root, _mask_token(token))

        app = ShimApp(
            driver=driver,
            api_key=token,
            allow_shared_kernel=config.allow_shared_kernel,
            default_template_id=default_template_id,
        )
        server = make_server(app, host=host, port=0)
        actual_port = server.server_address[1]
        # 動的ポートを公開（再起動でポートが変わっても .cube/port で解決可能）。
        write_port(paths, actual_port)
        _log.info(
            "shim_started port=%s driver=%s isolation=%s allow_shared_kernel=%s",
            actual_port,
            driver.name,
            driver.isolation_level,
            config.allow_shared_kernel,
        )
        return RunningShim(
            app=app,
            server=server,
            port=actual_port,
            lock=lock,
            paths=paths,
            driver=driver,
            idle_timeout=idle_timeout,
        )
    except BaseException:
        # 起動途中の失敗では flock を必ず解放する。
        lock.release()
        raise


def serve(
    *,
    paths: CubePaths,
    config: ShimConfig,
    driver: Driver,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    host: str = "127.0.0.1",
    default_template_id: str | None = None,
) -> None:
    """シムを起動してアイドル終了まで serve する（CLI ``subaco-shim serve`` の実体）。"""
    shim = start_shim(
        paths=paths,
        config=config,
        driver=driver,
        idle_timeout=idle_timeout,
        host=host,
        default_template_id=default_template_id,
    )
    try:
        shim.serve_forever()
    finally:
        shim.close()
