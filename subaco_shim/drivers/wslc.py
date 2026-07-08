"""wslc（WSL Containers）ドライバ（スケルトン・experimental）。

Windows の WSL Containers（2026-06-29 パブリックプレビュー、GA は 2026 年秋予定）を
バックエンドにする想定のドライバ。GA までは **experimental フラグ付き**で提供し、GA 後に
正式サポートへ昇格する。隔離レベルは ``shared-kernel``（default-deny では
ホスト管理者の ``allow_shared_kernel`` オプトイン時のみ実行可）。

呼び出し経路は WSL2 内プロセスから WSL 相互運用で wslc CLI を制御する。
プレビューで DoD を満たせない場合は **podman on WSL2 を Windows 既定**として出荷し、
wslc は GA 後の再検証タスクで必須昇格する。

本モジュールは **スケルトン**。各メソッドは platform 検出ガードのうえ
:class:`NotImplementedError` を送出する。**import は常に可能**。
"""

from __future__ import annotations

import platform
import shutil

from ..isolation import IsolationLevel
from ..models import Execution, SandboxInfo
from .base import Driver

# wslc CLI 名（プレビュー版。GA で確定予定）。
_WSLC_CLI = "wslc"

# TODO: WSL 相互運用経路（WSL2 内プロセス → wslc）の成立確認、egress 遮断下の
#   データプレーン到達とサンドボックス間分離の spike を反映し、_commands 相当を実装する。
# TODO(分岐): プレビューが DoD 未達なら podman on WSL2 を Windows 既定として出荷する。


class WslcDriver(Driver):
    """wslc ドライバ（shared-kernel・experimental スケルトン）。"""

    name = "wslc"
    isolation_level = IsolationLevel.SHARED_KERNEL

    #: experimental フラグ（GA 前は True。GA 追随で False へ）。
    experimental: bool = True

    @classmethod
    def available(cls) -> bool:
        """Windows/WSL 環境で ``wslc`` CLI を検出できるか。

        cube-shim 自体は WSL2（= x86_64-linux）内で動く前提。ネイティブ
        Windows では ``fcntl`` 等の Unix 機構が無く稼働しない。ここでは CLI 検出のみ判定する。
        """
        return shutil.which(_WSLC_CLI) is not None

    def _guard(self) -> None:
        """platform 検出ガード。ネイティブ Windows・CLI 不在では明示的に失敗させる。"""
        if platform.system() == "Windows":
            raise NotImplementedError(
                "cube-shim はネイティブ Windows 非対応です（WSL2 内で実行してください）。"
            )
        if shutil.which(_WSLC_CLI) is None:
            raise NotImplementedError("`wslc` CLI（WSL Containers プレビュー）が見つかりません。")

    def create(
        self,
        *,
        template_id: str,
        metadata: dict[str, str] | None = None,
    ) -> SandboxInfo:
        self._guard()
        raise NotImplementedError("WslcDriver.create は未実装")

    def exec(self, sandbox_id: str, code: str) -> Execution:
        self._guard()
        raise NotImplementedError("WslcDriver.exec は未実装")

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self._guard()
        raise NotImplementedError("WslcDriver.put_file は未実装")

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        self._guard()
        raise NotImplementedError("WslcDriver.get_file は未実装")

    def destroy(self, sandbox_id: str) -> None:
        self._guard()
        # TODO: サンドボックス破棄とネットワーク残骸掃除。
        raise NotImplementedError("WslcDriver.destroy は未実装")

    def get_info(self, sandbox_id: str) -> SandboxInfo:
        self._guard()
        raise NotImplementedError("WslcDriver.get_info は未実装")
