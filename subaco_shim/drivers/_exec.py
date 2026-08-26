"""CLI ドライバ共有の実行機構（サブプロセスの drain・上限・孤児検出）。

podman / Apple Container のように「バックエンド CLI をホスト側サブプロセスとして呼ぶ」
ドライバが共有する、バックエンド非依存の実行機構。ここに置くのは **ホスト側プロセスの
管理だけ**であり、どの CLI をどう呼ぶか（argv）は各ドライバの責務のまま。

含まれる振る舞いはすべてレビュー・実測で固めたもの（初出は podman ドライバ。経緯の
詳細は各 docstring）:

- **開始直後からの drain**: 待ってから読む方式は pipe 容量超の大量出力でデッドロックする。
- **出力上限**（バイト + 行イベント数）: 未信頼コードの出力し続けによるホスト OOM 防止。
- **ハード上限 timeout**: SDK 側 run_code(timeout) の保険（クライアント消失時）。
- **プロセスグループ kill と孤児検出**: 親正常終了後の子孫残存を killpg(pgid, 0) で検出し
  成功扱いにしない（pipe EOF では stdio を切り離した子孫が見えない——実測）。
- **stdin パイプの保持と閉鎖**: コンテナ内ウォッチドッグ（:func:`._commands.code_watchdog_wrapper`）
  へ EOF を届ける経路（切断キャンセルのコンテナ内到達）。

環境変数（全 CLI ドライバ共通）: ``SUBACO_SHIM_EXEC_TIMEOUT``（ハード上限秒。0 以下 =
無期限）、``SUBACO_SHIM_EXEC_MAX_OUTPUT``（系統別蓄積上限バイト。0 以下 = 無制限）。
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import subprocess
import threading
import time

from ..logging import get_logger
from ..models import Execution, ExecutionError, Logs, Result
from .base import ExecutionHandle

_log = get_logger("drivers.exec")

# 制御系 CLI コマンド（network / run / stop / rm / put / get）のタイムアウト（秒）。
DEFAULT_COMMAND_TIMEOUT = 120.0

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

# 「env から解決」と「明示 None（無期限/無制限）」を区別するための番兵。
_UNSET = object()


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


class CliExecutionHandle(ExecutionHandle):
    """CLI ``exec`` の Popen ハンドル。cancel はホスト側 exec プロセスを kill する。

    クライアント TCP 切断 = 実行キャンセル（spike §1.3）の実装。**実行開始時から
    reader スレッドが stdout/stderr を上限付きでドレーンする**——待ってから読む方式は
    pipe 容量超の大量出力でプロセスが write ブロックしたまま終了できずデッドロックし、
    無制限の蓄積は未信頼コードの出力し続けによるホスト OOM を許す。上限（``max_output``）
    超過分は**読み捨て**（pipe は読み続ける）、切り詰めの事実を stderr へ注記する。
    ドライバ側ハード上限（``timeout``。None = 無期限）は監視スレッドで実効化する
    （超過はプロセスグループごと SIGKILL → ExecTimeout）。exec プロセスの kill で
    コンテナ内プロセスまで確実に止まるかはバックエンド依存のため、実バックエンドごとの
    live スイート（podman nightly / Apple Container 実機）で検証する（コンテナ自体は
    destroy 時に停止・削除される）。

    **v0 の配信は一括**: イベントは実行完了後にまとめて JSON lines 化される（SDK の
    ``on_stdout`` 等へは完了後に届く）。逐次ストリーミングはドライバ抽象のストリーム化
    （M3 候補）で扱う。

    :class:`~subaco_shim.drivers.base.ExecutionHandle` の実装（done/result/cancel）。
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

        code_watchdog_wrapper の stdin 監視ウォッチドッグが EOF を検知して payload を
        kill する（exec クライアントの kill だけではコンテナ内プロセスが生き残ることを
        podman nightly 実測で確認——キャンセルのコンテナ内到達はこの EOF 経路が正）。
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
