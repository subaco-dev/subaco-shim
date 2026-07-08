"""ドライバ抽象基底クラス。

シムは単一のバックエンド抽象インターフェースを定義し、その実装として
``container``（Apple Container）/ ``wslc``（WSL Containers）/ ``podman`` の
3 ドライバを持つ。本モジュールは ABC のみを提供し、具体ドライバの
実装は後続段階で追加する。

抽象インターフェース: ``create`` / ``exec`` / ``put_file`` /
``get_file`` / ``destroy`` に、``get_info``（metadata に ``isolation_level`` を
含むサンドボックス情報返却）を加える。

**隔離レベルの保証**: 各ドライバは自身が提供する隔離レベル
:attr:`Driver.isolation_level` を 3 値のいずれか（``unknown`` ではない）で
宣言する。ローカル create で得る隔離レベルが必ず 3 値のいずれかであることを
シムが保証する根拠であり、``unknown`` は接続先未登録のリモート記録時のみ生じる。

このモジュールは stdlib と subaco_shim 内部のみに依存する（外部依存なしで import 可能）。
具体ドライバ（podman / container / wslc CLI）は実装時に遅延 import する想定。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..isolation import IsolationLevel
from ..models import Execution, SandboxInfo


class Driver(ABC):
    """バックエンドコンテナ機構のドライバ抽象基底。

    サブクラスは :attr:`name` と :attr:`isolation_level` を定義し、6 つの抽象
    メソッドを実装する。``exec`` は E2B の ``run_code`` に対応するデータプレーン
    実行であり、:class:`~subaco_shim.models.Execution` を返す。
    """

    #: ドライバ識別子（"podman" / "container" / "wslc" 等）。
    name: str = "base"

    #: このドライバが提供する隔離レベル。必ず 3 値のいずれか（unknown 禁止）。
    #: 例: podman/wslc = SHARED_KERNEL、container = VM_PER_CONTAINER。
    isolation_level: IsolationLevel = IsolationLevel.UNKNOWN

    @abstractmethod
    def create(
        self,
        *,
        template_id: str,
        metadata: dict[str, str] | None = None,
    ) -> SandboxInfo:
        """サンドボックスを作成し :class:`SandboxInfo` を返す。

        返す ``metadata`` には ``isolation_level``（= :attr:`isolation_level` の
        文字列）を必ず載せる（get_info と同一の値）。
        """
        raise NotImplementedError

    @abstractmethod
    def exec(self, sandbox_id: str, code: str) -> Execution:
        """サンドボックス内でコードを実行する（E2B ``run_code`` 相当のデータプレーン）。

        :class:`~subaco_shim.models.Execution` を返す（出力は ``.text``）。
        """
        raise NotImplementedError

    @abstractmethod
    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        """ホスト側データをサンドボックス内の ``path`` へ書き込む（envd files 相当）。

        ホストディレクトリのマウントは禁止。ファイル入出力は
        本メソッドと :meth:`get_file` 経由に限定する。
        """
        raise NotImplementedError

    @abstractmethod
    def get_file(self, sandbox_id: str, path: str) -> bytes:
        """サンドボックス内 ``path`` の内容を bytes で取得する（envd files 相当）。"""
        raise NotImplementedError

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None:
        """サンドボックスを破棄する。

        破棄時にサンドボックス個別ネットワークの残骸も掃除する（destroy 時のネットワーク削除）。
        """
        raise NotImplementedError

    @abstractmethod
    def get_info(self, sandbox_id: str) -> SandboxInfo:
        """サンドボックス情報を返す。

        ``metadata[isolation_level]`` に隔離レベルを載せる（隔離レベルの
        正典な返却経路。``X-Isolation-Level`` ヘッダはデバッグ用補助）。
        """
        raise NotImplementedError

    @classmethod
    def available(cls) -> bool:
        """このドライバがこのホストで利用可能か（バックエンド CLI の検出）。

        既定は False。具体ドライバが ``shutil.which`` 等で判定する
        （TODO: podman、container/wslc を実装）。
        """
        return False
