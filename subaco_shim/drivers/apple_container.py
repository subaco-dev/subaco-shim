"""Apple Container CLI ドライバ。

macOS 26 / Apple Silicon の Apple Container（``container`` CLI、v1.0 で CLI/API 凍結済み）を
**サブプロセス**として呼ぶ薄いドライバ。1 コンテナ = 1 軽量 VM のハイパーバイザレベル
隔離を提供し、隔離レベルは ``vm-per-container``——既定バックエンドの中で唯一、
共有カーネルに依らないため **オプトインなしで実行可**（default-deny で vm-per-container
以上は無条件許可）。

**検出**: PATH 上の ``container`` を優先検出し、無ければシステム既知パス
（/usr/local/bin——installer pkg の配置先）へフォールバックする。podman の PATH 優先と
同じ理由（ストレージ／サービスの所有者 = 環境が普段使う個体と一致させる）。加えて
``container`` は一般語のため、**macOS 以外では同名の無関係バイナリを誤検出しない**よう
:meth:`AppleContainerDriver.available` は Darwin 判定を先に置く（auto 選択は本ドライバを
最優先するので、この判定が無いと Linux で無関係の ``container`` を掴み得る）。

**前提**: macOS 26 以上（複数ネットワーク作成とコンテナ間分離は macOS 15 では成立
しない——Apple の technical overview に明記）。サービス（container-apiserver）の状態は
初回呼び出し時に ``container system status`` で確認し、不成立なら runbook 付きエラーを
返す。

**ネットワークとマウント**: サンドボックスごとに ``container network create --internal
cube-<id>``（ホスト専用 = egress なし・ホスト → データプレーン到達は維持）を個別作成し、
コンテナをそれに接続する。ホストディレクトリのマウントは禁止。destroy 時にネットワーク
残骸を掃除する。egress 遮断・サンドボックス間分離・切断キャンセルのコンテナ内到達は
**Apple Silicon 実機の live スイート**（tests/test_live_apple_container_isolation.py）で
実測する（M2b-1 の受け入れ条件。podman の nightly 実測——07_実測結果——の macOS 版）。

**CLI 不在でも import 可能**: バイナリ検出・前提チェックは呼び出し時（create 等）に行い、
モジュール import では失敗しない。
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import subprocess

from ..isolation import IsolationLevel
from ..logging import get_logger
from ..models import Execution, SandboxInfo
from . import _commands_apple as AC
from ._exec import (
    _UNSET,
    DEFAULT_COMMAND_TIMEOUT,
    CliExecutionHandle,
    resolve_exec_max_output,
    resolve_exec_timeout,
)
from .base import Driver

_log = get_logger("drivers.apple_container")

# PATH に container が無い場合のフォールバック探索パス（installer pkg の配置先）。
_SYSTEM_CONTAINER_PATHS = ("/usr/local/bin/container",)

# 複数ネットワーク・コンテナ間分離が成立する最低 macOS メジャーバージョン。
_MIN_MACOS_MAJOR = 26

# 前提が欠けたときに提示する runbook（受け入れ条件と同型——podman の RUNBOOK_ROOTLESS 参照）。
RUNBOOK_APPLE = (
    "Apple Container の前提が未整備です。以下を確認してください:\n"
    "  1) インストール: https://github.com/apple/container/releases の署名済み installer pkg\n"
    "     （v1.0 以上。`container --version` で確認）\n"
    "  2) サービス起動: `container system start`（初回はビルトインカーネルの取得を伴い得る）\n"
    "  3) 要件: macOS 26 以上 / Apple Silicon（複数ネットワークとサンドボックス間分離は\n"
    "     macOS 15 では成立しない）\n"
    "詳細は docs のセットアップ runbook を参照。"
)


class AppleContainerUnavailableError(RuntimeError):
    """container バイナリが見つからない・macOS 以外の場合に送出。"""


class AppleContainerPreflightError(RuntimeError):
    """実行の前提（macOS バージョン・サービス稼働）が欠ける場合に送出（runbook 付き）。"""


class AppleContainerCommandError(RuntimeError):
    """container サブコマンドが非ゼロ終了した場合に送出。"""

    def __init__(self, argv: list[str], returncode: int, stderr: bytes) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.decode("utf-8", "replace")
        super().__init__(
            f"container command failed (rc={returncode}): {' '.join(argv)}\n{self.stderr}"
        )


def _detect_binary() -> str | None:
    """PATH 上の container を優先し、無ければシステム既知パスへフォールバックする。

    PATH 優先の理由は podman ドライバと同じ（ストレージ／サービスの所有者と一致する
    個体を使う——複数インストール混在でのバージョン跨ぎ操作を構造的に避ける）。
    """
    which = shutil.which(AC.CONTAINER_CLI)
    if which is not None:
        return which
    for cand in _SYSTEM_CONTAINER_PATHS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _macos_major() -> int | None:
    """macOS のメジャーバージョンを返す（Darwin 以外・取得不能は None）。"""
    if platform.system() != "Darwin":
        return None
    ver = platform.mac_ver()[0]
    try:
        return int(ver.split(".", 1)[0])
    except (ValueError, IndexError):
        return None


class AppleContainerDriver(Driver):
    """Apple Container サブプロセスドライバ（隔離レベル = vm-per-container）。"""

    name = "container"
    isolation_level = IsolationLevel.VM_PER_CONTAINER

    def __init__(
        self,
        *,
        binary: str | None = None,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        exec_timeout: float | None | object = _UNSET,
        exec_max_output: int | None | object = _UNSET,
    ) -> None:
        # 検出は遅延（None のまま保持。呼び出し時に _ensure_ready で解決）。
        self._binary: str | None = binary
        # 制御系（network/run/stop 等）と run_code ハード上限は別物（podman ドライバと同じ
        # 規則——第一のタイムアウトは SDK 側 run_code(timeout)、こちらは保険）。
        self._command_timeout = command_timeout
        self._exec_timeout = resolve_exec_timeout() if exec_timeout is _UNSET else exec_timeout
        self._exec_max_output = (
            resolve_exec_max_output() if exec_max_output is _UNSET else exec_max_output
        )
        self._checked = False
        self._sandboxes: dict[str, SandboxInfo] = {}
        # 実行した container フル argv の記録（診断・テスト補助）。
        self.commands: list[list[str]] = []

    @classmethod
    def available(cls) -> bool:
        """macOS 上で container バイナリを検出できるか。

        Darwin 判定が先（``container`` は一般語のため、他 OS の同名バイナリを
        誤検出しない——モジュール docstring 参照）。
        """
        return platform.system() == "Darwin" and _detect_binary() is not None

    def _ensure_ready(self) -> str:
        """バイナリ検出と前提チェックを行い container パスを返す（初回のみ実チェック）。"""
        if platform.system() != "Darwin":
            _log.error("driver_unavailable driver=container reason=not-darwin")
            raise AppleContainerUnavailableError(
                "AppleContainerDriver は macOS 専用です（現在の platform では未対応）。"
            )
        binary = self._binary or _detect_binary()
        if binary is None:
            _log.error("driver_unavailable driver=container reason=binary-not-found")
            raise AppleContainerUnavailableError(
                f"`container` CLI が見つかりません（PATH／システムパスのいずれにも不在）。\n"
                f"{RUNBOOK_APPLE}"
            )
        self._binary = binary
        if not self._checked:
            major = _macos_major()
            if major is not None and major < _MIN_MACOS_MAJOR:
                _log.error(
                    "driver_preflight_failed driver=container reason=macos-version major=%s", major
                )
                raise AppleContainerPreflightError(
                    f"macOS {_MIN_MACOS_MAJOR} 以上が必要です（検出: macOS {major}。"
                    f"複数ネットワークとサンドボックス間分離が成立しません）。\n{RUNBOOK_APPLE}"
                )
            # サービス（container-apiserver）の稼働確認。未起動のまま create へ進むと
            # 分かりにくい失敗になるため、初回に runbook 付きで弾く。
            try:
                proc = subprocess.run(
                    [binary, "system", "status"],
                    capture_output=True,
                    timeout=self._command_timeout,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                _log.error(
                    "driver_preflight_failed driver=container reason=status-check detail=%s", exc
                )
                raise AppleContainerPreflightError(
                    f"`container system status` を確認できません: {exc}\n{RUNBOOK_APPLE}"
                ) from exc
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
                _log.error(
                    "driver_preflight_failed driver=container reason=service-not-running rc=%s",
                    proc.returncode,
                )
                raise AppleContainerPreflightError(
                    f"Apple Container サービスが稼働していません（`container system status` "
                    f"rc={proc.returncode}: {detail}）。\n{RUNBOOK_APPLE}"
                )
            self._checked = True
        return binary

    def _run(
        self,
        subargv: list[str],
        *,
        input: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """container サブコマンドを実行する。``check`` 時は非ゼロで例外。"""
        binary = self._binary or self._ensure_ready()
        argv = AC.full_argv(binary, subargv)
        self.commands.append(argv)
        try:
            proc = subprocess.run(
                argv,
                input=input,
                capture_output=True,
                timeout=self._command_timeout,
            )
        except FileNotFoundError as exc:
            _log.error("driver_command_failed driver=container reason=binary-not-found")
            raise AppleContainerUnavailableError(str(exc)) from exc
        if check and proc.returncode != 0:
            _log.error(
                "driver_command_failed driver=container rc=%s cmd=%s",
                proc.returncode,
                " ".join(subargv[:2]),
            )
            raise AppleContainerCommandError(argv, proc.returncode, proc.stderr)
        return proc

    # --- Driver インターフェース -----------------------------------------

    def create(
        self,
        *,
        template_id: str,
        metadata: dict[str, str] | None = None,
    ) -> SandboxInfo:
        self._ensure_ready()
        sandbox_id = secrets.token_hex(10)
        net = AC.network_name(sandbox_id)
        cont = AC.container_name(sandbox_id)
        # egress なしホスト専用ネットワークを個別作成 → ホストマウントなしでコンテナ起動。
        self._run(AC.create_network_argv(net))
        try:
            self._run(AC.run_container_argv(cont, net, template_id))
        except Exception:
            # コンテナ（VM）起動に失敗したら残骸を掃除する。run が失敗してもコンテナ記録が
            # 作られていることがある（podman の CI 実測と同型の防御）ため、
            # コンテナ → ネットワークの順に best-effort で削除する。
            self._run(AC.remove_container_argv(cont), check=False)
            self._run(AC.remove_network_argv(net), check=False)
            raise
        info = SandboxInfo(
            sandbox_id=sandbox_id,
            template_id=template_id,
            metadata=dict(metadata or {}),
        ).with_isolation_level(self.isolation_level)
        self._sandboxes[sandbox_id] = info
        return info

    def exec(self, sandbox_id: str, code: str) -> Execution:
        # ユーザコードは失敗し得るため rc 非ゼロも Execution（error 付き）として返す。
        return self.exec_start(sandbox_id, code).result()

    def exec_start(self, sandbox_id: str, code: str) -> CliExecutionHandle:
        """実行を開始し、kill 可能な Popen ハンドルを返す（切断キャンセル対応）。"""
        binary = self._binary or self._ensure_ready()
        cont = AC.container_name(sandbox_id)
        argv = AC.full_argv(binary, AC.exec_code_argv(cont, code))
        self.commands.append(argv)
        try:
            proc = subprocess.Popen(
                argv,
                # stdin はパイプで保持する（書き込まない）。stdin 監視ウォッチドッグへの
                # 供給路で、キャンセル／クライアント消滅時に閉じる（= コンテナ内 EOF →
                # payload kill）。DEVNULL だと即 EOF で誤発火する。
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # kill をプロセスグループ全体へ届かせる（孫プロセスの pipe 保持対策）。
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            _log.error("driver_command_failed driver=container reason=binary-not-found")
            raise AppleContainerUnavailableError(str(exc)) from exc
        return CliExecutionHandle(
            proc, timeout=self._exec_timeout, max_output=self._exec_max_output
        )

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        cont = AC.container_name(sandbox_id)
        self._run(AC.put_file_argv(cont, path), input=data)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        cont = AC.container_name(sandbox_id)
        proc = self._run(AC.get_file_argv(cont, path))
        return proc.stdout

    def destroy(self, sandbox_id: str) -> None:
        cont = AC.container_name(sandbox_id)
        net = AC.network_name(sandbox_id)
        # 停止・削除失敗はベストエフォート（掃除を継続する）。
        self._run(AC.stop_container_argv(cont), check=False)
        self._run(AC.remove_container_argv(cont), check=False)
        # destroy 時にネットワーク残骸を掃除する。
        self._run(AC.remove_network_argv(net), check=False)
        self._sandboxes.pop(sandbox_id, None)

    def get_info(self, sandbox_id: str) -> SandboxInfo:
        info = self._sandboxes.get(sandbox_id)
        if info is None:
            # 起動情報が手元に無い場合も隔離レベルは自ドライバの宣言値を返す（3 値保証）。
            info = SandboxInfo(sandbox_id=sandbox_id, template_id="").with_isolation_level(
                self.isolation_level
            )
        return info
