"""Apple Container ドライバ（スケルトン・experimental）。

macOS 26 / Apple Silicon の Apple Container（``container`` CLI、v1.0 で CLI/API 凍結済み）を
バックエンドにする想定のドライバ。1 コンテナ = 1 軽量 VM のハイパーバイザレベル隔離を提供し、
隔離レベルは ``vm-per-container``——既定バックエンドの中で唯一、
共有カーネルに依らないため **オプトインなしで実行可**（default-deny で vm-per-container 以上は
無条件許可）。

本モジュールは **スケルトン**。実装は Apple Silicon 実機で検証して行い、v0 では
platform 検出でガードしつつ各メソッドは :class:`NotImplementedError` を送出する。
CLI 体系は Docker 互換に収斂しているため、podman ドライバ（:mod:`.podman`）と同型の
argv ビルダ（:mod:`._commands` 相当）へ寄せられる見込み。

**import は常に可能**。``available`` は macOS + ``container`` CLI 検出時のみ True。
"""

from __future__ import annotations

import platform
import shutil

from ..isolation import IsolationLevel
from ..models import Execution, SandboxInfo
from .base import Driver

# Apple Container の CLI 名（v1.0 で凍結済み）。
_CONTAINER_CLI = "container"

# TODO: egress 遮断構成下のデータプレーン到達方式・サンドボックス間分離の成立を
#   Apple Silicon 実機の spike で確認し、_commands 相当の argv ビルダを実装する。


class AppleContainerDriver(Driver):
    """Apple Container ドライバ（vm-per-container・experimental スケルトン）。"""

    name = "container"
    isolation_level = IsolationLevel.VM_PER_CONTAINER

    @classmethod
    def available(cls) -> bool:
        """macOS 上で ``container`` CLI を検出できるか。"""
        return platform.system() == "Darwin" and shutil.which(_CONTAINER_CLI) is not None

    def _guard(self) -> None:
        """platform 検出ガード。macOS 以外・CLI 不在では明示的に失敗させる。"""
        if platform.system() != "Darwin":
            raise NotImplementedError(
                "AppleContainerDriver は macOS 専用です（現在の platform では未対応）。"
            )
        if shutil.which(_CONTAINER_CLI) is None:
            raise NotImplementedError("`container` CLI（Apple Container v1.0）が見つかりません。")

    def create(
        self,
        *,
        template_id: str,
        metadata: dict[str, str] | None = None,
    ) -> SandboxInfo:
        self._guard()
        # TODO: `container run` 相当でサンドボックス個別 VM を起動し、
        #   metadata に isolation_level=vm-per-container を載せて返す。
        raise NotImplementedError("AppleContainerDriver.create は未実装")

    def exec(self, sandbox_id: str, code: str) -> Execution:
        self._guard()
        raise NotImplementedError("AppleContainerDriver.exec は未実装")

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self._guard()
        raise NotImplementedError("AppleContainerDriver.put_file は未実装")

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        self._guard()
        raise NotImplementedError("AppleContainerDriver.get_file は未実装")

    def destroy(self, sandbox_id: str) -> None:
        self._guard()
        # TODO: VM 破棄とネットワーク残骸掃除。
        raise NotImplementedError("AppleContainerDriver.destroy は未実装")

    def get_info(self, sandbox_id: str) -> SandboxInfo:
        self._guard()
        raise NotImplementedError("AppleContainerDriver.get_info は未実装")
