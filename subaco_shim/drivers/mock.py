"""実コンテナ不要の契約テスト用モックドライバ（CI のユニット／契約テストの主役）。

ドライバ抽象を **in-memory** で満たす。実 podman を呼ばず、代わりに
:mod:`subaco_shim.drivers._commands` が組み立てる podman 風 argv を ``commands`` に記録し、
「ネットワークの個別作成／掃除」の呼び出しを検証可能にする。

主な検証支援フィールド:

- :attr:`MockDriver.commands`         : 実行した podman 風フル argv の列（記録順）。
- :attr:`MockDriver.created_networks` : create で作成したネットワーク名の列。
- :attr:`MockDriver.removed_networks` : destroy で削除したネットワーク名の列。
- :attr:`MockDriver.live_networks`    : 現在生存しているネットワーク名の集合（リーク検出用）。

隔離レベルはコンストラクタで差し替え可能（既定 ``shared-kernel``）。default-deny
ルーティングのテスト（オプトイン要否・vm-per-container 許可）で使い分ける。

stdlib のみに依存し、外部依存なしで import・動作する。
"""

from __future__ import annotations

import secrets
import time

from ..isolation import IsolationLevel
from ..models import Execution, Logs, Result, SandboxInfo
from . import _commands as C
from .base import Driver


class MockSandboxNotFoundError(KeyError):
    """未知の sandbox_id を操作しようとした場合に送出。"""


class MockFileNotFoundError(KeyError):
    """get_file で未書き込みのパスを読もうとした場合に送出。"""


class MockDriver(Driver):
    """契約テスト用の in-memory ドライバ。"""

    name = "mock"

    def __init__(self, *, isolation_level: IsolationLevel = IsolationLevel.SHARED_KERNEL) -> None:
        # 宣言隔離レベルはテストで差し替え可能（既定は shared-kernel）。
        self.isolation_level = isolation_level
        # 記録用（ネットワーク個別作成／掃除を検証可能にする）。
        self.commands: list[list[str]] = []
        self.created_networks: list[str] = []
        self.removed_networks: list[str] = []
        self.live_networks: set[str] = set()
        # in-memory 状態。
        self._sandboxes: dict[str, SandboxInfo] = {}
        self._files: dict[tuple[str, str], bytes] = {}

    # --- 実装補助 ---------------------------------------------------------

    def _record(self, subargv: list[str]) -> None:
        self.commands.append(C.full_argv(C.PODMAN, subargv))

    def _require(self, sandbox_id: str) -> SandboxInfo:
        try:
            return self._sandboxes[sandbox_id]
        except KeyError as exc:
            raise MockSandboxNotFoundError(sandbox_id) from exc

    @classmethod
    def available(cls) -> bool:
        # モックは常に利用可能（CI/契約テストの主役）。
        return True

    # --- Driver インターフェース -----------------------------------------

    def create(
        self,
        *,
        template_id: str,
        metadata: dict[str, str] | None = None,
    ) -> SandboxInfo:
        sandbox_id = secrets.token_hex(10)
        net = C.network_name(sandbox_id)
        cont = C.container_name(sandbox_id)
        # ネットワーク個別作成 → そのネットワークでコンテナ起動（ホストマウントなし）。
        self._record(C.create_network_argv(net))
        self.created_networks.append(net)
        self.live_networks.add(net)
        self._record(C.run_container_argv(cont, net, template_id))
        info = SandboxInfo(
            sandbox_id=sandbox_id,
            template_id=template_id,
            metadata=dict(metadata or {}),
        ).with_isolation_level(self.isolation_level)
        self._sandboxes[sandbox_id] = info
        return info

    def exec(self, sandbox_id: str, code: str) -> Execution:
        self._require(sandbox_id)
        cont = C.container_name(sandbox_id)
        self._record(C.exec_code_argv(cont, code))
        # 決定的な擬似出力（コードをそのまま主結果テキストとして返す）。
        return Execution(
            results=[Result(text=code, is_main_result=True)],
            logs=Logs(stdout=[code]),
        )

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self._require(sandbox_id)
        cont = C.container_name(sandbox_id)
        self._record(C.put_file_argv(cont, path))
        self._files[(sandbox_id, path)] = bytes(data)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        self._require(sandbox_id)
        cont = C.container_name(sandbox_id)
        self._record(C.get_file_argv(cont, path))
        try:
            return self._files[(sandbox_id, path)]
        except KeyError as exc:
            raise MockFileNotFoundError(path) from exc

    def destroy(self, sandbox_id: str) -> None:
        info = self._require(sandbox_id)
        cont = C.container_name(sandbox_id)
        net = C.network_name(sandbox_id)
        # コンテナ停止／削除 → ネットワーク残骸の掃除。
        self._record(C.stop_container_argv(cont))
        self._record(C.remove_container_argv(cont))
        self._record(C.remove_network_argv(net))
        self.removed_networks.append(net)
        self.live_networks.discard(net)
        info.ended_at = info.ended_at or time.time()
        del self._sandboxes[sandbox_id]
        # 当該サンドボックスのファイルも破棄。
        for key in [k for k in self._files if k[0] == sandbox_id]:
            del self._files[key]

    def get_info(self, sandbox_id: str) -> SandboxInfo:
        return self._require(sandbox_id)
