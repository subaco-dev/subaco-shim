"""ドライバ抽象基底クラス。

シムは単一のバックエンド抽象インターフェースを定義し、その実装として
``container``（Apple Container）/ ``wslc``（WSL Containers）/ ``podman`` の
3 ドライバを持つ。本モジュールは ABC のみを提供し、具体ドライバの
実装は後続段階で追加する。

抽象インターフェース: ``create`` / ``exec`` / ``put_file`` /
``get_file`` / ``destroy`` に、``get_info``（metadata に ``isolation_level`` を
含むサンドボックス情報返却）と ``exec_start``（キャンセル可能な実行ハンドル——
run_code の「クライアント TCP 切断 = 実行キャンセル」に必要）を加える。

**隔離レベルの保証**: 各ドライバは自身が提供する隔離レベル
:attr:`Driver.isolation_level` を 3 値のいずれか（``unknown`` ではない）で
宣言する。ローカル create で得る隔離レベルが必ず 3 値のいずれかであることを
シムが保証する根拠であり、``unknown`` は接続先未登録のリモート記録時のみ生じる。

このモジュールは stdlib と subaco_shim 内部のみに依存する（外部依存なしで import 可能）。
具体ドライバ（podman / container / wslc CLI）は実装時に遅延 import する想定。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from ..isolation import IsolationLevel
from ..models import Execution, SandboxInfo


class ExecutionHandle(ABC):
    """実行中コードへのハンドル（完了待機とキャンセル）。

    run_code の「クライアント TCP 切断 = 実行キャンセル」（spike §1.3）を成立させるため、
    シムは実行を本ハンドル経由で待機し、切断検出時に :meth:`cancel` を呼ぶ。
    """

    @abstractmethod
    def done(self) -> bool:
        """実行が完了（またはキャンセル）したか。ブロックしない。"""
        raise NotImplementedError

    @abstractmethod
    def result(self) -> Execution:
        """完了を待って :class:`Execution` を返す（キャンセル済みはエラー Execution）。"""
        raise NotImplementedError

    @abstractmethod
    def cancel(self) -> None:
        """実行を中止する（冪等）。実プロセスドライバはバックエンドプロセスを停止すること。"""
        raise NotImplementedError


class ThreadedExecutionHandle(ExecutionHandle):
    """同期 ``exec`` をスレッドで走らせる既定ハンドル。

    **cancel は記録のみで実行を停止できない**（Python スレッドは外部から割り込めない）。
    実プロセスを持つドライバ（podman 等）は必ず :meth:`Driver.exec_start` を
    プロセス kill 可能な形でオーバーライドすること。
    """

    def __init__(self, fn: Callable[[], Execution]) -> None:
        self._result: Execution | None = None
        self._exc: BaseException | None = None
        self._finished = threading.Event()
        self.cancelled = False
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], Execution]) -> None:
        try:
            self._result = fn()
        except BaseException as exc:  # result() で呼び出し側スレッドへ再送出する。
            self._exc = exc
        finally:
            self._finished.set()

    def done(self) -> bool:
        return self._finished.is_set()

    def result(self) -> Execution:
        self._finished.wait()
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    def cancel(self) -> None:
        # スレッドは停止できない（記録のみ）。実プロセスドライバでオーバーライドする。
        self.cancelled = True


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

    def exec_start(self, sandbox_id: str, code: str) -> ExecutionHandle:
        """実行を開始しキャンセル可能なハンドルを返す（クライアント切断時の実行停止用）。

        既定実装は :class:`ThreadedExecutionHandle`（**cancel は記録のみ**）。実プロセスを
        持つドライバはプロセス kill を実装したハンドルを返すこと（podman 参照）。
        """
        return ThreadedExecutionHandle(lambda: self.exec(sandbox_id, code))

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
