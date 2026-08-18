"""podman CLI ドライバ。

podman を **サブプロセス**として呼ぶ薄いドライバ。隔離レベルは ``shared-kernel``
（共有カーネル層。default-deny ではホスト管理者の ``allow_shared_kernel`` オプトイン時のみ
実行が許可される）。

**podman 検出**: **PATH 上の podman を優先検出**し、無ければシステム既知パス
（/usr/bin 等）へフォールバックする。PATH 優先なのは、rootless のコンテナストレージは
「環境が普段使う podman」＝ PATH 解決される個体が所有するためで、別の個体を選ぶと
複数インストール環境（例: GitHub ランナーの /usr/local 同梱 5.x + apt の 4.9）で
バージョン混在のストレージアクセスになり ``podman run`` がハングする（CI 実測）。
非 NixOS Linux の rootless 前提（``/etc/subuid``・``/etc/subgid``・setuid 付き
``newuidmap``）はバイナリの出自と独立にホスト側で必要で、欠ける場合は
:class:`PodmanPreflightError` に runbook を添えて送出する。

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

import contextlib
import math
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..isolation import IsolationLevel
from ..logging import get_logger
from ..models import Execution, ExecutionError, Logs, Result, SandboxInfo
from . import _commands as C
from .base import Driver, ExecutionHandle

_log = get_logger("drivers.podman")

# PATH に podman が無い場合のフォールバック探索パス（PATH 優先——_detect_binary 参照）。
_SYSTEM_PODMAN_PATHS = ("/usr/bin/podman", "/usr/local/bin/podman", "/opt/podman/bin/podman")

# 制御系 podman コマンド（network / run / stop / rm / put / get）のタイムアウト（秒）。
_DEFAULT_COMMAND_TIMEOUT = 120.0

# run_code ハード上限の既定（秒）。**第一のタイムアウトは SDK 側の run_code(timeout=...)**
# （read タイムアウト → 切断 → シムがキャンセル）であり、これはクライアント消失・切断検出
# 漏れ時に未信頼コードを走らせ続けないための保険。SUBACO_SHIM_EXEC_TIMEOUT で上書き可
# （0 以下 = 無効 / 無期限）。
_DEFAULT_EXEC_TIMEOUT = 3600.0

# 実行出力（stdout/stderr 各系統）のホスト側蓄積上限の既定（バイト）。未信頼コードの
# 出力し続けによるホスト OOM を防ぐ。超過分は読み捨て（pipe は読み続けるためデッド
# ロックしない）、切り詰めの事実を stderr へ注記する。SUBACO_SHIM_EXEC_MAX_OUTPUT で
# 上書き可（0 以下 = 無制限）。
_DEFAULT_EXEC_MAX_OUTPUT = 10 * 1024 * 1024

# 行イベント数の系統別上限。バイト上限を通過した出力でも、短い行の大量分割は
# str オブジェクトのオーバーヘッドで数十倍に膨張する（10MiB の 5 バイト行 →
# 約 210MB 実測）ため、後処理（splitlines 相当）にもメモリ境界を置く。
# 上限を超えた残りは 1 要素に集約して保持する（データは失わない）。
_MAX_OUTPUT_LINES = 10_000

# reader スレッドの読み取り単位。
_READ_CHUNK = 65536


def _env_float(name: str, default: float) -> float:
    """有限の float のみ受理する（nan/inf・解析不能は警告して既定値）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        val = None
    if val is None or not math.isfinite(val):
        _log.warning("invalid_env_value name=%s value=%r using_default=%s", name, raw, default)
        return default
    return val


def _env_int(name: str, default: int) -> int:
    """整数のみ受理する（小数・nan/inf・解析不能は警告して既定値）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("invalid_env_value name=%s value=%r using_default=%s", name, raw, default)
        return default


def resolve_exec_timeout() -> float | None:
    """run_code ハード上限を解決する（env 上書き可。0 以下は None = 無期限）。"""
    val = _env_float("SUBACO_SHIM_EXEC_TIMEOUT", _DEFAULT_EXEC_TIMEOUT)
    return None if val <= 0 else val


def resolve_exec_max_output() -> int | None:
    """実行出力の蓄積上限を解決する（env 上書き可・整数のみ。0 以下は None = 無制限）。"""
    val = _env_int("SUBACO_SHIM_EXEC_MAX_OUTPUT", _DEFAULT_EXEC_MAX_OUTPUT)
    return None if val <= 0 else val


# str.splitlines() が行境界として扱う文字の集合（\r\n は 1 境界として先に照合）。
_LINE_BOUNDARY = re.compile("\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def _split_lines_bounded(text: str, max_lines: int = _MAX_OUTPUT_LINES) -> list[str]:
    """splitlines() の行分割に後処理メモリ境界を置く: 上限を超えた残りは 1 要素に集約する。

    上限内の結果は ``str.splitlines()`` と同一（LF だけでなく CR/CRLF/VT/FF/FS/GS/RS/
    NEL/LS/PS の全境界を扱う）。境界は finditer で遅延走査するため巨大な行リストを
    実体化せず、残り全体は 1 つの str のまま保持する（要素あたりのオーバーヘッドが
    掛からず、メモリはバイト上限と同じオーダーに収まる）。データは失わない。
    """
    if not text:
        return []
    lines: list[str] = []
    pos = 0
    for m in _LINE_BOUNDARY.finditer(text):
        if len(lines) >= max_lines:
            break
        lines.append(text[pos : m.start()])
        pos = m.end()
    if pos < len(text):
        lines.append(text[pos:])  # 最終行（終端なし）または集約された残り（境界を含む）。
    return lines


# 「env から解決」と「明示 None（無期限/無制限）」を区別するための番兵。
_UNSET = object()

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
    """PATH 上の podman を優先し、無ければシステム既知パスへフォールバックする。

    **PATH 優先の理由（CI 実測で確定した障害の再発防止）**: rootless のコンテナ
    ストレージ（``~/.local/share/containers``）は「環境が普段使う podman」＝ PATH 解決
    される個体が所有・初期化する。複数の podman が併存する環境（GitHub ランナーは
    /usr/local/bin に 5.x を同梱し、apt は /usr/bin に 4.9 を入れる）で PATH と別の
    個体を選ぶと、**別バージョンが同一ストレージを跨いで操作し ``podman run`` が
    futex 待ちのまま無出力でハングする**（バージョン混在のロック/DB 非互換。
    stderr も出ないため 120s タイムアウトまで沈黙する）。ハードコードパスの優先は
    この混在を構造的に招くため、フォールバック専用とする。
    """
    which = shutil.which("podman")
    if which is not None:
        for cand in _SYSTEM_PODMAN_PATHS:
            if cand != which and os.path.isfile(cand) and os.access(cand, os.X_OK):
                # PATH 優先＝ストレージ所有者と一致させる（docstring 参照）。
                _log.info("mixed_podman_installs detected=%s using=%s", cand, which)
                break
        return which
    for cand in _SYSTEM_PODMAN_PATHS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


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


class PodmanExecutionHandle(ExecutionHandle):
    """``podman exec`` の Popen ハンドル。cancel はホスト側 exec プロセスを kill する。

    クライアント TCP 切断 = 実行キャンセル（spike §1.3）の実装。**実行開始時から
    reader スレッドが stdout/stderr を上限付きでドレーンする**——待ってから読む方式は
    pipe 容量超の大量出力でプロセスが write ブロックしたまま終了できずデッドロックし、
    無制限の蓄積は未信頼コードの出力し続けによるホスト OOM を許す。上限（``max_output``）
    超過分は**読み捨て**（pipe は読み続ける）、切り詰めの事実を stderr へ注記する。
    ドライバ側ハード上限（``timeout``。None = 無期限）は監視スレッドで実効化する
    （超過はプロセスグループごと SIGKILL → ExecTimeout）。exec プロセスの kill で
    コンテナ内プロセスまで確実に止まるかはバックエンド依存のため、実コンテナでの検証は
    podman nightly / 実機統合の対象（コンテナ自体は destroy 時に停止・削除される）。

    **v0 の配信は一括**: イベントは実行完了後にまとめて JSON lines 化される（SDK の
    ``on_stdout`` 等へは完了後に届く）。逐次ストリーミングはドライバ抽象のストリーム化
    （M3 候補）で扱う。
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        *,
        timeout: float | None,
        max_output: int | None,
    ) -> None:
        self._proc = proc
        self._timeout = timeout
        self._max_output = max_output
        self._cancelled = False
        self._timed_out = False
        self._orphaned = False
        self._stdout_buf = bytearray()
        self._stderr_buf = bytearray()
        self._truncated = [False, False]
        self._finished = threading.Event()
        self._readers = [
            threading.Thread(
                target=self._read_stream, args=(proc.stdout, self._stdout_buf, 0), daemon=True
            ),
            threading.Thread(
                target=self._read_stream, args=(proc.stderr, self._stderr_buf, 1), daemon=True
            ),
        ]
        for t in self._readers:
            t.start()
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _read_stream(self, stream: object, buf: bytearray, idx: int) -> None:
        """pipe を EOF まで読み続ける（上限到達後は読み捨て——書き手をブロックさせない）。"""
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                return
            if self._max_output is None:
                buf.extend(chunk)
                continue
            room = self._max_output - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if room < len(chunk):
                self._truncated[idx] = True

    def _close_stdin(self) -> None:
        """stdin パイプを閉じてコンテナ内ウォッチドッグへ EOF を届ける（冪等）。

        exec_code_argv の stdin 監視ウォッチドッグが EOF を検知して payload を kill する
        （podman exec クライアントの kill だけではコンテナ内プロセスが生き残ることを
        nightly 実測で確認——キャンセルのコンテナ内到達はこの EOF 経路が正）。
        """
        stdin = self._proc.stdin
        if stdin is not None:
            with contextlib.suppress(OSError):
                stdin.close()

    def _kill_group(self) -> None:
        """exec プロセスを**プロセスグループごと** kill する。

        ``proc.kill()`` は直接の子しか殺さないため、シェルパイプライン等の孫プロセスが
        stdout の write 端を保持し続けると EOF が来ず drain が終わらない。exec_start は
        ``start_new_session=True`` で起動しており、グループ全体を SIGKILL できる。
        あわせて stdin を閉じ、コンテナ内 payload の停止（ウォッチドッグ EOF）を届ける。
        """
        self._close_stdin()
        try:
            os.killpg(self._proc.pid, 9)  # SIGKILL
        except (ProcessLookupError, PermissionError, OSError):
            self._proc.kill()

    def _group_alive(self) -> bool:
        """プロセスグループに生存メンバーがいるか（``killpg(pgid, 0)`` の存在確認）。

        exec_start は ``start_new_session=True`` で起動するため pgid == 親 pid。親は
        wait() で reap 済みなので、ここで見えるのは親が残した子孫だけ。ProcessLookupError
        以外（PermissionError 等）は「存在するが送れない」なので生存扱い（保守側）。
        """
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _kill_group_and_wait(self, timeout: float) -> bool:
        """グループを SIGKILL し、全メンバーの消滅（PID 消失）まで待つ。消えれば True。"""
        self._kill_group()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._group_alive():
                return True
            time.sleep(0.01)
        return not self._group_alive()

    def _join_readers(self, timeout: float) -> bool:
        """reader の終了（= 両 pipe の EOF）を待つ。全員終われば True。"""
        deadline = time.monotonic() + timeout
        for t in self._readers:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self._readers)

    def _watch(self) -> None:
        """プロセス完了とハード上限を監視する（pipe とは独立の wait ベース）。"""
        try:
            self._proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            _log.warning("exec_timeout pid=%s timeout=%ss", self._proc.pid, self._timeout)
            self._kill_group()
            self._proc.wait()
        # 親終了後の子孫残存は**プロセスグループの生存**で検出する。pipe の EOF は
        # 「write 端を保持する子孫がいない」ことしか示さず、stdio を /dev/null 等へ
        # 向けた子孫は EOF では見えない。残存子孫は出力欠落・ハード上限回避の経路
        # なのでグループごと停止し（PID 消滅まで確認）、**成功扱いにしない**
        # （result は OrphanedProcesses エラー）。子孫が無ければ killpg(0) が即
        # ProcessLookupError になるだけで、正常系のコストはゼロ。
        if self._group_alive():
            self._orphaned = True
            _log.warning("exec_orphaned_processes pid=%s — killing process group", self._proc.pid)
            if not self._kill_group_and_wait(timeout=5.0):
                _log.error("exec_group_kill_stuck pid=%s", self._proc.pid)
        # 両 pipe の EOF（= write 端の全クローズ）を待って出力を確定する。グループ停止後
        # は速やかに EOF が来るはず。来ない場合も子孫残存（グループ外へ逃れた等）として
        # 扱い、成功にしない。
        if not self._join_readers(timeout=2.0):
            self._orphaned = True
            _log.warning("exec_drain_not_eof pid=%s — killing process group", self._proc.pid)
            self._kill_group()
            if not self._join_readers(timeout=5.0):
                # killpg 後も残る異常系（daemon スレッドのため後始末はプロセス終了時）。
                _log.error("exec_drain_stuck pid=%s", self._proc.pid)
        # 正常完了でも stdin パイプを解放する（キャンセル系は _kill_group が閉鎖済み）。
        self._close_stdin()
        self._finished.set()

    def done(self) -> bool:
        return self._finished.is_set()

    def result(self) -> Execution:
        self._finished.wait()
        stdout = bytes(self._stdout_buf).decode("utf-8", "replace")
        stderr = bytes(self._stderr_buf).decode("utf-8", "replace")
        # 行分割にも上限を置く（バイト上限通過後の splitlines() は短い行の大量出力で
        # 数十倍に再膨張する——_split_lines_bounded 参照）。
        logs = Logs(
            stdout=_split_lines_bounded(stdout),
            stderr=_split_lines_bounded(stderr),
        )
        if any(self._truncated):
            _log.warning(
                "exec_output_truncated pid=%s max_output=%s", self._proc.pid, self._max_output
            )
            logs.stderr.append(f"[output truncated at {self._max_output} bytes per stream]")
        if self._cancelled:
            return Execution(
                logs=logs,
                error=ExecutionError(name="Cancelled", value="client disconnected"),
            )
        if self._timed_out:
            return Execution(
                logs=logs,
                error=ExecutionError(name="ExecTimeout", value=f"timeout={self._timeout}s"),
            )
        if self._orphaned:
            # 親は正常終了したが子孫が残っていた（グループごと停止済み）。出力が欠けて
            # いる可能性があり、ハード上限の回避経路でもあるため成功扱いにしない。
            return Execution(
                logs=logs,
                error=ExecutionError(
                    name="OrphanedProcesses",
                    value="sandbox processes outlived the exec entrypoint and were killed",
                ),
            )
        if self._proc.returncode != 0:
            return Execution(
                results=[],
                logs=logs,
                error=ExecutionError(
                    name="ExecError", value=stderr or f"rc={self._proc.returncode}"
                ),
            )
        return Execution(results=[Result(text=stdout, is_main_result=True)], logs=logs)

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        _log.info("exec_cancelled pid=%s", self._proc.pid)
        self._kill_group()


class PodmanDriver(Driver):
    """podman サブプロセスドライバ（隔離レベル = shared-kernel）。"""

    name = "podman"
    isolation_level = IsolationLevel.SHARED_KERNEL

    def __init__(
        self,
        *,
        binary: str | None = None,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        exec_timeout: float | None | object = _UNSET,
        exec_max_output: int | None | object = _UNSET,
    ) -> None:
        # 検出は遅延（None のまま保持。呼び出し時に _ensure_ready で解決）。
        self._binary: str | None = binary
        # 制御系（network/run/stop 等）と run_code ハード上限は別物: 前者は固定短時間、
        # 後者は SDK タイムアウトの保険（env で調整可・無効化可——モジュール先頭コメント）。
        self._command_timeout = command_timeout
        self._exec_timeout = resolve_exec_timeout() if exec_timeout is _UNSET else exec_timeout
        self._exec_max_output = (
            resolve_exec_max_output() if exec_max_output is _UNSET else exec_max_output
        )
        self._checked = False
        self._sandboxes: dict[str, SandboxInfo] = {}
        # 実行した podman フル argv の記録（診断・テスト補助）。
        self.commands: list[list[str]] = []

    @classmethod
    def available(cls) -> bool:
        """podman バイナリを検出できるか（PATH 優先検出——_detect_binary 参照）。"""
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
                timeout=self._command_timeout,
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
            # コンテナ起動に失敗したら残骸を掃除する。run がタイムアウト等で失敗しても
            # コンテナ記録が作られていることがある（実測: CI の 120s タイムアウト）ため、
            # コンテナ → ネットワークの順に best-effort で削除する。
            self._run(C.remove_container_argv(cont), check=False)
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
        # ユーザコードは失敗し得るため rc 非ゼロも Execution（error 付き）として返す。
        return self.exec_start(sandbox_id, code).result()

    def exec_start(self, sandbox_id: str, code: str) -> PodmanExecutionHandle:
        """実行を開始し、kill 可能な Popen ハンドルを返す（切断キャンセル対応）。"""
        binary = self._binary or self._ensure_ready()
        cont = C.container_name(sandbox_id)
        argv = C.full_argv(binary, C.exec_code_argv(cont, code))
        self.commands.append(argv)
        try:
            proc = subprocess.Popen(
                argv,
                # stdin はパイプで保持する（書き込まない）。exec_code_argv の stdin 監視
                # ウォッチドッグへの供給路で、キャンセル／クライアント消滅時に閉じる
                # （= コンテナ内 EOF → payload kill）。DEVNULL だと即 EOF で誤発火する。
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # kill をプロセスグループ全体へ届かせる（孫プロセスの pipe 保持対策）。
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            _log.error("driver_command_failed driver=podman reason=binary-not-found")
            raise PodmanUnavailableError(str(exc)) from exc
        return PodmanExecutionHandle(
            proc, timeout=self._exec_timeout, max_output=self._exec_max_output
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
