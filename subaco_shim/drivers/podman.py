"""podman CLI ドライバ。

podman を **サブプロセス**として呼ぶ薄いドライバ。隔離レベルは ``shared-kernel``
（共有カーネル層。default-deny ではホスト管理者の ``allow_shared_kernel`` オプトイン時のみ
実行が許可される）。

**podman 検出**: 非 NixOS Linux の rootless 実行はホスト側前提（``/etc/subuid``・
``/etc/subgid``・setuid 付き ``newuidmap``）を要し、nixpkgs 版配布だけでは完結しない。
そのため cube-shim は**システムインストール済み podman を優先検出**し、無ければ PATH 上の
podman を使う。前提が欠ける場合は :class:`PodmanPreflightError` に runbook を添えて送出する。

**ネットワークとマウント**: サンドボックスごとに ``podman network create --internal
cube-<id>`` で egress なし内部ネットワークを個別作成し、コンテナをそれに接続する。
``--network=none`` は使わない（ホスト → データプレーンの TCP 到達性を維持するため）。
ホストディレクトリのマウントは禁止。destroy 時にネットワーク残骸を掃除する。

**podman 不在でも import 可能**: バイナリ検出・前提チェックは呼び出し時（create 等）に行い、
モジュール import では失敗しない。実際のコンテナ実行は受け入れ条件どおり
Linux CI（ubuntu ランナー）で検証する。このマシン（macOS・podman 無し）では
:meth:`PodmanDriver.available` は False を返す。
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import subprocess
from pathlib import Path

from ..isolation import IsolationLevel
from ..logging import get_logger
from ..models import Execution, ExecutionError, Logs, Result, SandboxInfo
from . import _commands as C
from .base import Driver

_log = get_logger("drivers.podman")

# システムインストール済み podman の優先探索パス（システム podman を優先検出）。
_SYSTEM_PODMAN_PATHS = ("/usr/bin/podman", "/usr/local/bin/podman", "/opt/podman/bin/podman")

# 既定の実行タイムアウト（秒）。run_code は将来ストリーミング対応で見直す（TODO）。
_DEFAULT_TIMEOUT = 120.0

# 非 NixOS Linux rootless 前提が欠けたときに提示する runbook（受け入れ条件）。
RUNBOOK_ROOTLESS = (
    "podman rootless 前提が未整備です（非 NixOS Linux）。以下を確認してください:\n"
    "  1) /etc/subuid・/etc/subgid に現在ユーザのサブ ID 範囲が登録されているか\n"
    "     （例: `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`）\n"
    "  2) distro の uidmap パッケージが提供する setuid 付き newuidmap/newgidmap が存在するか\n"
    "     （例: Debian/Ubuntu は `sudo apt install uidmap`）\n"
    "  3) 反映後に `podman system migrate` を実行\n"
    "詳細は docs のセットアップ runbook を参照。"
    "GitHub Actions の ubuntu ランナーは設定済みのため CI では通常問題になりません。"
)


class PodmanUnavailableError(RuntimeError):
    """podman バイナリが見つからない場合に送出。"""


class PodmanPreflightError(RuntimeError):
    """rootless 実行の前提（subuid/subgid・newuidmap）が欠ける場合に送出（runbook 付き）。"""


class PodmanCommandError(RuntimeError):
    """podman サブコマンドが非ゼロ終了した場合に送出。"""

    def __init__(self, argv: list[str], returncode: int, stderr: bytes) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.decode("utf-8", "replace")
        super().__init__(
            f"podman command failed (rc={returncode}): {' '.join(argv)}\n{self.stderr}"
        )


def _detect_binary() -> str | None:
    """システム podman を優先し、無ければ PATH 上の podman を返す。無ければ None。"""
    for cand in _SYSTEM_PODMAN_PATHS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("podman")


def _is_nixos() -> bool:
    """NixOS 上かどうか（rootless 前提チェックの対象外にするため）。"""
    if Path("/etc/NIXOS").exists():
        return True
    try:
        return "nixos" in Path("/etc/os-release").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _current_user_ids() -> tuple[str, str]:
    """現在ユーザ名と uid 文字列を返す（/etc/subuid 照合に使う）。"""
    uid = os.getuid()
    try:
        import pwd

        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        name = str(uid)
    return name, str(uid)


def _subid_registered(subid_file: str, user: str, uid: str) -> bool:
    """``/etc/subuid`` / ``/etc/subgid`` に user もしくは uid のエントリがあるか。"""
    try:
        for line in Path(subid_file).read_text(encoding="utf-8").splitlines():
            head = line.split(":", 1)[0].strip()
            if head in (user, uid):
                return True
    except OSError:
        return False
    return False


def _has_setuid(name: str) -> bool:
    """``newuidmap`` 等が存在し setuid ビットを持つか。"""
    path = shutil.which(name)
    if not path:
        return False
    try:
        import stat

        return bool(os.stat(path).st_mode & stat.S_ISUID)
    except OSError:
        return False


def check_rootless_prerequisites() -> list[str]:
    """非 NixOS Linux rootless 前提の欠落項目を英語キーで返す。

    Linux 以外・NixOS では空リスト（対象外）。欠落があれば呼び出し側が
    :class:`PodmanPreflightError`（+ :data:`RUNBOOK_ROOTLESS`）として送出する。
    """
    if platform.system() != "Linux" or _is_nixos():
        return []
    problems: list[str] = []
    user, uid = _current_user_ids()
    if not _subid_registered("/etc/subuid", user, uid):
        problems.append("missing-subuid")
    if not _subid_registered("/etc/subgid", user, uid):
        problems.append("missing-subgid")
    if not _has_setuid("newuidmap"):
        problems.append("missing-setuid-newuidmap")
    if not _has_setuid("newgidmap"):
        problems.append("missing-setuid-newgidmap")
    return problems


class PodmanDriver(Driver):
    """podman サブプロセスドライバ（隔離レベル = shared-kernel）。"""

    name = "podman"
    isolation_level = IsolationLevel.SHARED_KERNEL

    def __init__(self, *, binary: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        # 検出は遅延（None のまま保持。呼び出し時に _ensure_ready で解決）。
        self._binary: str | None = binary
        self._timeout = timeout
        self._checked = False
        self._sandboxes: dict[str, SandboxInfo] = {}
        # 実行した podman フル argv の記録（診断・テスト補助）。
        self.commands: list[list[str]] = []

    @classmethod
    def available(cls) -> bool:
        """podman バイナリを検出できるか（システム優先検出）。"""
        return _detect_binary() is not None

    def _ensure_ready(self) -> str:
        """バイナリ検出と rootless 前提チェックを行い podman パスを返す（初回のみ実チェック）。"""
        binary = self._binary or _detect_binary()
        if binary is None:
            _log.error("driver_unavailable driver=podman reason=binary-not-found")
            raise PodmanUnavailableError(
                "podman バイナリが見つかりません（システム／PATH のいずれにも不在）。"
            )
        self._binary = binary
        if not self._checked:
            problems = check_rootless_prerequisites()
            if problems:
                _log.error("driver_preflight_failed driver=podman problems=%s", ",".join(problems))
                raise PodmanPreflightError(
                    f"rootless 前提が不足: {', '.join(problems)}\n{RUNBOOK_ROOTLESS}"
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
        """podman サブコマンドを実行する。``check`` 時は非ゼロで例外。"""
        binary = self._binary or self._ensure_ready()
        argv = C.full_argv(binary, subargv)
        self.commands.append(argv)
        try:
            proc = subprocess.run(
                argv,
                input=input,
                capture_output=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            _log.error("driver_command_failed driver=podman reason=binary-not-found")
            raise PodmanUnavailableError(str(exc)) from exc
        if check and proc.returncode != 0:
            _log.error(
                "driver_command_failed driver=podman rc=%s cmd=%s",
                proc.returncode,
                " ".join(subargv[:2]),
            )
            raise PodmanCommandError(argv, proc.returncode, proc.stderr)
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
        net = C.network_name(sandbox_id)
        cont = C.container_name(sandbox_id)
        # egress なし内部ネットワークを個別作成 → ホストマウントなしでコンテナ起動。
        self._run(C.create_network_argv(net))
        try:
            self._run(C.run_container_argv(cont, net, template_id))
        except Exception:
            # コンテナ起動に失敗したらネットワーク残骸を掃除する。
            self._run(C.remove_network_argv(net), check=False)
            raise
        info = SandboxInfo(
            sandbox_id=sandbox_id,
            template_id=template_id,
            metadata=dict(metadata or {}),
        ).with_isolation_level(self.isolation_level)
        self._sandboxes[sandbox_id] = info
        return info

    def exec(self, sandbox_id: str, code: str) -> Execution:
        cont = C.container_name(sandbox_id)
        # ユーザコードは失敗し得るため check=False。返値から Execution を組み立てる。
        proc = self._run(C.exec_code_argv(cont, code), check=False)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        logs = Logs(
            stdout=stdout.splitlines() if stdout else [],
            stderr=stderr.splitlines() if stderr else [],
        )
        if proc.returncode != 0:
            return Execution(
                results=[],
                logs=logs,
                error=ExecutionError(name="ExecError", value=stderr or f"rc={proc.returncode}"),
            )
        return Execution(
            results=[Result(text=stdout, is_main_result=True)],
            logs=logs,
        )

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        cont = C.container_name(sandbox_id)
        self._run(C.put_file_argv(cont, path), input=data)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        cont = C.container_name(sandbox_id)
        proc = self._run(C.get_file_argv(cont, path))
        return proc.stdout

    def destroy(self, sandbox_id: str) -> None:
        cont = C.container_name(sandbox_id)
        net = C.network_name(sandbox_id)
        # 停止・削除失敗はベストエフォート（掃除を継続する）。
        self._run(C.stop_container_argv(cont), check=False)
        self._run(C.remove_container_argv(cont), check=False)
        # destroy 時にネットワーク残骸を掃除する。
        self._run(C.remove_network_argv(net), check=False)
        self._sandboxes.pop(sandbox_id, None)

    def get_info(self, sandbox_id: str) -> SandboxInfo:
        info = self._sandboxes.get(sandbox_id)
        if info is None:
            # 起動情報が手元に無い場合も隔離レベルは自ドライバの宣言値を返す（3 値保証）。
            info = SandboxInfo(sandbox_id=sandbox_id, template_id="").with_isolation_level(
                self.isolation_level
            )
        return info
