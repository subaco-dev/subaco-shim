#!/usr/bin/env python3
"""sandbox_run.py — 隔離環境でエージェント生成コードを検証する（M2a-4 本実装）。

============================================================================
このファイルは **リファレンス（同梱コピー）** です。
**正典（canonical）はテンプレート側（subaco の multi-agent テンプレート）** に置かれ、
プロジェクトへ scaffold されます（テンプレート組み込みは M2b-3）。shim リポジトリには、
SDK 契約・構造化出力形状の参照実装として同梱しています。
============================================================================

役割（協業ループ）:

    エージェントA がコード生成 → sandbox_run で検証 → 結果を「隔離レベル込み」で
    hive_remember(kind=finding) に記録 → エージェントB が hive_recall で参照。

このスクリプトは **検証結果と隔離レベルを構造化 JSON で返すだけ**で、hive への記録は
呼び出し元エージェントが hive_remember で行う（単一ライター境界）。

単一契約: 接続先が cube-shim / CubeSandbox / ホステッド E2B のいずれでも
本コードは無改変で動く。``E2B_DOMAIN`` が設定されていれば非 shim 接続とみなし、
シムのオンデマンド起動・接続解決は行わない。

- **オンデマンド起動と接続の実行時解決**: `.envrc` の export は direnv 評価時の
  スナップショットに過ぎないため、本スクリプトは**実行直前に** ``.cube/port`` /
  ``.cube/token`` / ``.cube/tls/ca-bundle.pem`` を読み直して環境変数を更新する。
  シム未稼働（初回・アイドル終了後）なら起動して接続可能になるまで待つ。
  起動コマンドは ``CUBE_SHIM_CMD`` → PATH の ``cube-shim`` → ``subaco-shim serve``。
- **リトライ**: サンドボックス作成の**接続確立前と確定できる失敗のみ**再試行する
  （connection refused / httpx.ConnectError / ConnectTimeout）。**ReadTimeout は
  再試行しない** — 要求がサーバー側で完了した後に応答だけ失われた可能性があり、
  create の再送はサンドボックス（コンテナ／ネットワーク）を孤児化させる。
  実行済みコードの再送もしない（/execute の再送は SDK 側でも起きない——spike §1.3）。
- **post-create フック**: サンドボックス ID 取得後・データプレーン接続前に補助処理を
  挟める（``post_create_hook`` 引数 / ``CUBE_POST_CREATE_CMD`` 環境変数）。
  systemd-resolved 非稼働環境の /etc/hosts フォールバック追記に使う（README runbook）。
- **タイムアウト**: 実行タイムアウトは run_code に渡す。SDK はチャンク間無通信時間
  （read タイムアウト）として扱う（spike §1.3）。既定は SDK の 300 秒。
- **隔離レベル**: get_info の metadata（正典経路）から取得。無ければ fail-closed:
  非 shim 接続先（``E2B_DOMAIN``）は ``~/.config/subaco-shim/config.toml`` の
  登録済みリモートのみ ``microvm-dedicated-kernel``、それ以外は **unknown**。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 隔離レベルキー（get_info の metadata に載る値）。
ISOLATION_LEVEL_KEY = "isolation_level"
# fail-closed の既定（metadata に無い＝隔離保証なしとして扱う）。
UNKNOWN_ISOLATION = "unknown"
# 登録済みリモート接続先の隔離レベル（設計書 §5.3 の fail-closed 判定）。
REGISTERED_REMOTE_ISOLATION = "microvm-dedicated-kernel"

# サンドボックス作成の再試行既定（回数と初回待機秒。待機は指数バックオフ）。
DEFAULT_CREATE_RETRIES = 3
DEFAULT_RETRY_WAIT = 0.2
# シムのオンデマンド起動を待つ既定秒数（コールドキャッシュ・遅い CI ランナーを考慮。
# CUBE_SHIM_STARTUP_WAIT で上書き可）。
DEFAULT_SHIM_STARTUP_WAIT = 60.0
# post-create フックコマンドのタイムアウト秒。
POST_CREATE_CMD_TIMEOUT = 30.0


def _default_sandbox_factory() -> Callable[..., Any]:
    """e2b_code_interpreter.Sandbox を **遅延 import** で返す（未導入でも本 module は import 可）。

    外部依存（e2b SDK）が import できなくてもモジュール自体は壊れない。
    """
    from e2b_code_interpreter import Sandbox  # type: ignore[import-not-found]

    return Sandbox.create


# --- 接続の実行時解決とオンデマンド起動 ---------------------------------------


def _find_cube_dir() -> Path:
    """``.cube`` を特定する（``CUBE_DIR`` 明示 → CWD から上方探索 → CWD/.cube）。"""
    explicit = os.environ.get("CUBE_DIR")
    if explicit:
        return Path(explicit)
    d = Path.cwd()
    while True:
        cand = d / ".cube"
        if cand.is_dir():
            return cand
        if d.parent == d:
            return Path.cwd() / ".cube"
        d = d.parent


def _read_cube_port(cube_dir: Path) -> int | None:
    try:
        return int((cube_dir / "port").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _shim_reachable(cube_dir: Path) -> bool:
    """``.cube/port`` のシムへ TCP 接続できるか（稼働判定）。"""
    port = _read_cube_port(cube_dir)
    if port is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _shim_launcher() -> list[str] | None:
    """シム起動コマンド（``CUBE_SHIM_CMD`` → ``cube-shim`` → ``subaco-shim serve``）。"""
    cmd = os.environ.get("CUBE_SHIM_CMD")
    if cmd:
        return shlex.split(cmd)
    exe = shutil.which("cube-shim")
    if exe:
        return [exe]
    exe = shutil.which("subaco-shim")
    if exe:
        return [exe, "serve"]
    return None


def _ensure_shim_running(cube_dir: Path, *, wait: float | None = None) -> None:
    """シムをオンデマンド起動し、接続可能になるまで待つ（稼働中なら何もしない）。

    多重起動は flock（単一インスタンス）で 2 個目が即終了するため、起動の試行自体は
    常に安全。診断出力は ``.cube/shim.log`` へ追記する。起動プロセスが接続可能になる前に
    終了した場合は待ち切らず即エラーにする（原因はログを参照）。
    """
    if wait is None:
        try:
            wait = float(os.environ.get("CUBE_SHIM_STARTUP_WAIT", DEFAULT_SHIM_STARTUP_WAIT))
        except ValueError:
            wait = DEFAULT_SHIM_STARTUP_WAIT
    if _shim_reachable(cube_dir):
        return
    launcher = _shim_launcher()
    if launcher is None:
        raise RuntimeError(
            "シムが未稼働で、起動コマンドも見つかりません"
            "（CUBE_SHIM_CMD を設定するか cube-shim / subaco-shim を PATH に置いてください）"
        )
    cube_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = cube_dir / "shim.log"
    with log_path.open("ab") as log:
        proc = subprocess.Popen(  # noqa: S603 - launcher はホスト側設定由来
            launcher,
            cwd=str(cube_dir.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _shim_reachable(cube_dir):
            return
        if proc.poll() is not None:
            # 起動プロセスが終了。並行起動の敗者（flock で即終了）の可能性があるため、
            # 少しだけ稼働側の出現を待ってからエラーにする。
            grace = time.monotonic() + 2.0
            while time.monotonic() < grace:
                if _shim_reachable(cube_dir):
                    return
                time.sleep(0.1)
            tail = _tail_text(log_path)
            raise RuntimeError(
                f"シム起動プロセスが接続可能になる前に終了しました"
                f"（rc={proc.returncode}）。{log_path} 末尾:\n{tail}"
            )
        time.sleep(0.1)
    raise RuntimeError(f"シムの起動を {wait} 秒待ちましたが接続できません（{log_path} を確認）")


def _tail_text(path: Path, limit: int = 2000) -> str:
    """ログ末尾を診断用に読む（読めなければプレースホルダ）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return "(ログを読めません)"
    return data[-limit:].decode("utf-8", "replace") or "(空)"


def _resolve_connection(cube_dir: Path) -> None:
    """接続情報を ``.cube`` から読み直して環境変数を更新する（実行直前の再解決）。

    再起動でポートが変わっても、この再解決により呼び出しが陳腐化しない。
    """
    port = _read_cube_port(cube_dir)
    if port is not None:
        os.environ["E2B_API_URL"] = f"http://127.0.0.1:{port}"
    try:
        token = (cube_dir / "token").read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if token:
        os.environ["E2B_API_KEY"] = token
    bundle = cube_dir / "tls" / "ca-bundle.pem"
    if bundle.is_file():
        os.environ["SSL_CERT_FILE"] = str(bundle)


# --- 作成リトライ（接続確立前と確定できる失敗のみ） ---------------------------


def _is_retryable_create_error(exc: Exception) -> bool:
    """作成時の例外が**接続確立前と確定できる失敗**（再試行安全）か。

    ReadTimeout・ConnectionReset 等は要求がサーバー側で処理された後の可能性があり、
    create の再送はサンドボックスを孤児化させるため再試行しない。httpx は e2b SDK の
    依存として存在する前提だが、遅延 import で不在でも壊れない。
    """
    if isinstance(exc, ConnectionRefusedError):
        return True
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        return False
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


def _create_with_retry(
    factory: Callable[..., Any],
    *,
    template: str | None,
    retries: int,
    retry_wait: float,
    reconnect: Callable[[], None] | None = None,
) -> Any:
    """サンドボックスを作成する。接続確立前と確定できる失敗のみ指数バックオフで再試行する。

    ``reconnect`` は再試行の直前に呼ばれ、シムの再起動確認と接続情報の再解決を行う
    （アイドル終了後の旧ポートからの回復経路）。
    """
    attempt = 0
    while True:
        try:
            return factory(template=template)
        except Exception as exc:
            if attempt >= retries or not _is_retryable_create_error(exc):
                raise
            time.sleep(retry_wait * (2**attempt))
            if reconnect is not None:
                reconnect()
            attempt += 1


# --- 隔離レベル（fail-closed） ------------------------------------------------


def _extract_isolation_level(info: Any) -> str:
    """get_info 返却から隔離レベル文字列を取り出す（無ければ unknown — fail-closed）。

    E2B SDK の get_info 返却（SandboxInfo）は metadata 属性を持つ。接続先・バージョン
    差異に備えて dict 形も受ける。
    """
    metadata = getattr(info, "metadata", None)
    if metadata is None and isinstance(info, dict):
        metadata = info.get("metadata")
    if isinstance(metadata, dict):
        level = metadata.get(ISOLATION_LEVEL_KEY)
        if level:
            return str(level)
    return UNKNOWN_ISOLATION


def _shim_config_path() -> Path:
    """シム設定の既定パス（``~/.config/subaco-shim/config.toml``。XDG を尊重）。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "subaco-shim" / "config.toml"


def _registered_remote_level(domain: str) -> str:
    """非 shim 接続先の fail-closed 判定（設計書 §5.3）。

    ホスト管理者の ``config.toml``（エージェント書換不能パス）に**登録済み**の接続先のみ
    ``microvm-dedicated-kernel``、未登録・設定不在・読取失敗は ``unknown``。
    """
    try:
        import tomllib

        with _shim_config_path().open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return UNKNOWN_ISOLATION
    domains: set[str] = set()
    for entry in data.get("remote", []) or []:
        if isinstance(entry, dict) and entry.get("domain"):
            domains.add(str(entry["domain"]))
    for dom in data.get("trusted_remotes", []) or []:
        domains.add(str(dom))
    return REGISTERED_REMOTE_ISOLATION if domain in domains else UNKNOWN_ISOLATION


# --- Execution の構造化 --------------------------------------------------------


def _execution_to_dict(execution: Any) -> dict[str, Any] | None:
    """Execution を JSON 化可能な dict へ（実 e2b SDK は to_json のみ、骨子は to_dict を持つ）。"""
    to_dict = getattr(execution, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    to_json = getattr(execution, "to_json", None)
    if callable(to_json):
        try:
            parsed = json.loads(to_json())
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# --- post-create フック --------------------------------------------------------


def _run_post_create(sandbox: Any, hook: Callable[[Any], None] | None) -> None:
    """サンドボックス ID 取得後・データプレーン接続前の補助処理を実行する。

    ``hook`` 引数が最優先。無ければ ``CUBE_POST_CREATE_CMD``（コマンドに sandbox_id を
    引数として渡して実行。非ゼロ終了は実行中断）。/etc/hosts フォールバック追記
    （README runbook）等、名前解決の準備に使う。
    """
    if hook is not None:
        hook(sandbox)
        return
    cmd = os.environ.get("CUBE_POST_CREATE_CMD")
    if cmd:
        subprocess.run(  # noqa: S603 - コマンドはホスト側設定由来
            [*shlex.split(cmd), str(sandbox.sandbox_id)],
            check=True,
            timeout=POST_CREATE_CMD_TIMEOUT,
        )


# --- 本体 ---------------------------------------------------------------------


def run_untrusted(
    code: str,
    *,
    template_id: str | None = None,
    sandbox_factory: Callable[..., Any] | None = None,
    timeout: float | None = None,
    retries: int = DEFAULT_CREATE_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT,
    post_create_hook: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """コードを隔離環境で実行し、hive_remember に渡せる構造化 dict を返す。

    引数:
        code: 実行するコード（エージェント生成の未信頼コード）。
        template_id: OCI テンプレート参照（既定は環境変数 ``CUBE_TEMPLATE_ID``）。
        sandbox_factory: ``Sandbox.create`` 相当（テスト時に差替可。既定は e2b を遅延 import。
            注入時はシムのオンデマンド起動・接続解決を行わない）。
        timeout: 実行タイムアウト秒（run_code に渡す。None は SDK 既定 300 秒）。
        retries: 作成時の接続確立前失敗の最大再試行回数。
        retry_wait: 再試行の初回待機秒（指数バックオフ）。
        post_create_hook: create 後・run_code 前に呼ぶ補助処理（無ければ
            ``CUBE_POST_CREATE_CMD``）。

    返り値（構造化出力）:
        ``ok`` / ``text`` / ``isolation_level`` / ``template_id`` / ``execution`` / ``error``。
    """
    template = template_id or os.environ.get("CUBE_TEMPLATE_ID")

    result: dict[str, Any] = {
        "ok": False,
        "text": None,
        ISOLATION_LEVEL_KEY: UNKNOWN_ISOLATION,
        "template_id": template,
        "execution": None,
        "error": None,
    }

    # E2B_DOMAIN 設定時は非 shim 接続（CubeSandbox / ホステッド E2B）。factory 注入時は
    # テスト経路。いずれもシムのオンデマンド起動・接続解決は行わない。
    remote_domain = os.environ.get("E2B_DOMAIN")
    use_shim = sandbox_factory is None and not remote_domain
    cube_dir = _find_cube_dir() if use_shim else None

    def _reconnect() -> None:
        assert cube_dir is not None
        _ensure_shim_running(cube_dir)
        _resolve_connection(cube_dir)

    try:
        if cube_dir is not None:
            _reconnect()
        factory = sandbox_factory or _default_sandbox_factory()
        with _create_with_retry(
            factory,
            template=template,
            retries=retries,
            retry_wait=retry_wait,
            reconnect=_reconnect if cube_dir is not None else None,
        ) as sb:
            # ID 取得後・データプレーン接続前の補助処理（/etc/hosts フォールバック等）。
            _run_post_create(sb, post_create_hook)
            # timeout=None は kwargs ごと省略し SDK 既定（300 秒）に委ねる。
            run_kwargs = {} if timeout is None else {"timeout": timeout}
            execution = sb.run_code(code, **run_kwargs)
            # run_code は Execution を返す。出力は .text、構造化は .to_dict()/.to_json()。
            # str(Execution) は repr を返すため使わない。
            result["text"] = getattr(execution, "text", None)
            result["execution"] = _execution_to_dict(execution)
            # 隔離レベルは get_info の metadata から取得（正典な返却経路）。無ければ
            # fail-closed（非 shim 接続先は登録済みリモートのみ microvm 扱い）。
            level = _extract_isolation_level(sb.get_info())
            if level == UNKNOWN_ISOLATION and remote_domain:
                level = _registered_remote_level(remote_domain)
            result[ISOLATION_LEVEL_KEY] = level
            result["ok"] = getattr(execution, "error", None) is None
    except Exception as exc:  # noqa: BLE001 - 呼び出し元へ構造化エラーで返す
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    """コードを stdin（または引数）で受け取り、構造化 JSON を stdout に出力する。"""
    parser = argparse.ArgumentParser(
        prog="sandbox_run",
        description="隔離環境でコードを検証し、構造化 JSON（隔離レベル込み）を出力する",
    )
    parser.add_argument("code", nargs="?", default=None, help="実行コード（省略時は stdin）")
    parser.add_argument(
        "--template",
        default=None,
        help="OCI テンプレート参照（既定は環境変数 CUBE_TEMPLATE_ID）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="実行タイムアウト秒（既定は SDK の 300 秒）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_CREATE_RETRIES,
        help="作成時の接続確立前失敗の最大再試行回数",
    )
    args = parser.parse_args(argv)

    code = args.code if args.code is not None else sys.stdin.read()
    payload = run_untrusted(
        code, template_id=args.template, timeout=args.timeout, retries=args.retries
    )
    # hive_remember へそのまま渡せる構造化 JSON（記録は呼び出し元が行う）。
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
