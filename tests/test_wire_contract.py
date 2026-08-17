"""E2B ワイヤ契約テスト: 実 SDK（固定バージョン）をクライアントに使う E2E。

spike（docs/00-memo/05_spike結果_E2B_ワイヤ.md・判定 full-fidelity-feasible）の決定実験
``spikes/e2b-wire/spike_rundcode_tls.py`` を pytest 化したもの。**SDK 無改造・root 不要・
外部 DNS 不要**で create → files → run_code → get_info → kill の全系統がシムに対して
green になることを検証する（e2b==2.30.0 / e2b-code-interpreter==2.8.1 に pin——pyproject）。

実行条件（いずれかを欠く環境では skip）:

- ``e2b_code_interpreter`` が導入済み（``uv sync --extra test``）。
- ``openssl`` CLI（TLS 証明書の生成に必要）。
- ``*.sbx.localhost`` が 127.0.0.1 へ解決されること（macOS / systemd-resolved 稼働 Linux は
  解決される。コンテナ等の非稼働環境は不解決——spike §7。その場合はフォールバック
  〔/etc/hosts への追記〕が必要で、本テストは skip して理由を表示する）。
"""

from __future__ import annotations

import shutil
import threading

import pytest

from subaco_shim.config import CubePaths, ShimConfig
from subaco_shim.drivers.mock import MockDriver
from subaco_shim.isolation import IsolationLevel
from subaco_shim.lifecycle import check_subdomain_resolution, start_shim
from subaco_shim.tokens import read_token

e2b_code_interpreter = pytest.importorskip(
    "e2b_code_interpreter", reason="実 SDK 契約テストは uv sync --extra test の環境でのみ実行"
)

pytestmark = [
    pytest.mark.skipif(
        shutil.which("openssl") is None, reason="TLS 証明書生成に openssl CLI が必要"
    ),
    pytest.mark.skipif(
        not check_subdomain_resolution(),
        reason="*.sbx.localhost が 127.0.0.1 へ解決されない環境"
        "（systemd-resolved 非稼働。/etc/hosts フォールバック手順は README 参照）",
    ),
]


@pytest.fixture(scope="module")
def live_shim(tmp_path_factory):
    """シムを実起動し、SDK の接続先環境変数を配線する（.envrc / M2a-6 と同じ構成）。

    module スコープの単一インスタンス: SDK（httpx）は共有トランスポートの SSL context を
    プロセス内でキャッシュするため、テストごとに証明書を作り直すと 2 個目以降のシムを
    信頼できない。実運用（1 プロジェクト = 1 シム・証明書永続）と同じ形になる。
    """
    mp = pytest.MonkeyPatch()
    paths = CubePaths.resolve(tmp_path_factory.mktemp("wire"))
    shim = start_shim(
        paths=paths,
        config=ShimConfig(),  # allow_shared_kernel=False（既定）
        driver=MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER),
        idle_timeout=0,
        default_template_id="tmpl-default",
    )
    thread = threading.Thread(
        target=shim.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    # .envrc 相当の配線: E2B_API_URL（平文制御プレーン）・E2B_API_KEY（.cube/token）・
    # SSL_CERT_FILE（certifi 結合バンドル）。E2B_DEBUG / E2B_SANDBOX_URL は不使用。
    mp.setenv("E2B_API_URL", f"http://127.0.0.1:{shim.port}")
    mp.setenv("E2B_API_KEY", read_token(paths))
    mp.setenv("SSL_CERT_FILE", str(shim.tls.ca_bundle))
    for var in ("E2B_DEBUG", "E2B_SANDBOX_URL", "E2B_DOMAIN"):
        mp.delenv(var, raising=False)
    try:
        yield shim
    finally:
        mp.undo()
        shim.shutdown()
        thread.join(timeout=5)
        shim.close()


def test_full_lifecycle_with_real_sdk(live_shim):
    """create → run_code → files write/read → get_info → kill の全系統を実 SDK で往復する。"""
    from e2b_code_interpreter import Sandbox

    sbx = Sandbox.create(template="code-interpreter-v1", metadata={"purpose": "contract"})
    try:
        assert sbx.sandbox_id
        # domain はポート埋め込み形（データプレーン単一 TLS リスナー）。
        assert sbx.sandbox_domain == f"sbx.localhost:{live_shim.data_port}"

        # run_code（POST /execute の chunked JSON lines を SDK が解釈する）。
        execution = sbx.run_code("1+1")
        # MockDriver はコードをそのまま主結果・stdout として返す。
        assert execution.text == "1+1"
        assert execution.logs.stdout == ["1+1"]
        assert execution.error is None

        # files（envd 面の multipart write → 生バイト read）。
        sbx.files.write("/tmp/hello.txt", "hello subaco")
        assert sbx.files.read("/tmp/hello.txt") == "hello subaco"

        # get_info（SandboxDetail 必須 10 キー + metadata round-trip）。
        info = sbx.get_info()
        assert info.sandbox_id == sbx.sandbox_id
        assert info.metadata["isolation_level"] == "vm-per-container"
        assert info.metadata["purpose"] == "contract"
    finally:
        # kill: 204 → True。二重 kill は 404 → False（例外なし）。
        assert sbx.kill() is True


def test_kill_unknown_sandbox_returns_false(live_shim):
    from e2b_code_interpreter import Sandbox

    sbx = Sandbox.create(template="tmpl-x")
    assert sbx.kill() is True
    assert sbx.kill() is False  # 404 → False（例外なし——spike §1.1）


def test_run_code_error_events(live_shim):
    """error イベント（name/value/traceback）が SDK の Execution.error に写ること。"""
    from e2b_code_interpreter import Sandbox

    # MockDriver に error を返させる: driver.exec を差し替える。
    driver = live_shim.driver
    from subaco_shim.models import Execution, ExecutionError, Logs

    original = driver.exec
    driver.exec = lambda sid, code: Execution(
        logs=Logs(stdout=["before boom\n"]),
        error=ExecutionError(name="NameError", value="name 'boom' is not defined", traceback="tb"),
        execution_count=7,
    )
    try:
        sbx = Sandbox.create(template="tmpl-x")
        try:
            execution = sbx.run_code("boom")
            assert execution.error is not None
            assert execution.error.name == "NameError"
            assert execution.logs.stdout == ["before boom\n"]
            assert execution.execution_count == 7
        finally:
            sbx.kill()
    finally:
        driver.exec = original


def test_wrong_api_key_raises_authentication_error(live_shim, monkeypatch):
    from e2b import exceptions
    from e2b_code_interpreter import Sandbox

    monkeypatch.setenv("E2B_API_KEY", "e2b_" + "f" * 32)
    with pytest.raises(exceptions.AuthenticationException):
        Sandbox.create(template="tmpl-x")


def _load_sandbox_run():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "sandbox_run.py"
    spec = importlib.util.spec_from_file_location("sandbox_run_e2e", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sandbox_run_end_to_end(live_shim, monkeypatch):
    """sandbox_run.py（M2a-4）が実 SDK → シム経由で構造化出力を返すこと。"""
    # 稼働中の live_shim の .cube を明示（CWD 上方探索で別シムを起動させない）。
    monkeypatch.setenv("CUBE_DIR", str(live_shim.paths.root))
    mod = _load_sandbox_run()

    result = mod.run_untrusted("print('hello')", template_id="tmpl-x")
    assert result["ok"] is True, result
    assert result["text"] == "print('hello')"  # MockDriver はコードをそのまま返す
    assert result["isolation_level"] == "vm-per-container"
    assert result["template_id"] == "tmpl-x"
    # hive_remember にそのまま渡せる JSON 化可能な構造化出力。
    assert isinstance(result["execution"], dict)
    import json as _json

    _json.dumps(result)


def test_run_code_timeout_cancels_backend_execution(live_shim):
    """SDK の実行タイムアウト（= クライアント切断）でバックエンド実行がキャンセルされること。

    レビュー指摘の再現: 従来は driver.exec() 完了後に初めて応答を開始していたため、
    クライアントが TimeoutException になってもバックエンドは実行を継続していた。
    """
    import threading
    import time

    from e2b import exceptions
    from e2b_code_interpreter import Sandbox

    driver = live_shim.driver
    driver.exec_gate = threading.Event()  # set されるまで実行が完了しない。
    try:
        sbx = Sandbox.create(template="tmpl-x")
        with pytest.raises(exceptions.TimeoutException):
            sbx.run_code("slow", timeout=1)
        # 切断検出でバックエンド実行がキャンセルされる（実プロセス停止の契約）。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sbx.sandbox_id not in driver.cancelled_execs:
            time.sleep(0.05)
        assert sbx.sandbox_id in driver.cancelled_execs
    finally:
        driver.exec_gate.set()
        driver.exec_gate = None
    assert sbx.kill() is True


def test_sandbox_run_on_demand_startup_and_restart(live_shim, tmp_path, monkeypatch):
    """初回（.cube 未初期化）とアイドル終了後（stale port）の両方から回復すること。

    TLS 資材は live_shim と共有する: SDK（httpx）の SSL context はプロセス内で
    キャッシュされるため、別証明書の新シムはこのプロセスから信頼できない。
    実運用では証明書がプロジェクト永続なので同じ前提が成り立つ。
    """
    import os
    import shlex
    import shutil as _shutil
    import signal
    import socket
    import sys
    import time

    proj = tmp_path
    cube = proj / ".cube"
    (cube / "tls").mkdir(parents=True)
    for name in ("cert.pem", "key.pem", "ca-bundle.pem"):
        _shutil.copy(live_shim.paths.tls_dir / name, cube / "tls" / name)
    (cube / "tls" / "key.pem").chmod(0o600)

    # CLI の --driver mock は既定 shared-kernel のため、サブプロセスのシムに
    # ホスト管理者オプトイン（allow_shared_kernel）を XDG 設定で与える。
    xdg = proj / "xdg"
    (xdg / "subaco-shim").mkdir(parents=True)
    (xdg / "subaco-shim" / "config.toml").write_text(
        "allow_shared_kernel = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    serve_cmd = (
        f"{shlex.quote(sys.executable)} -m subaco_shim.cli serve --driver mock "
        f"--project-root {shlex.quote(str(proj))} --idle-timeout 60"
    )
    wrapper = shlex.join(["bash", "-c", f"echo $$ > {proj}/shim.pid; exec {serve_cmd}"])
    monkeypatch.setenv("CUBE_SHIM_CMD", wrapper)
    monkeypatch.setenv("CUBE_DIR", str(cube))
    mod = _load_sandbox_run()

    def _kill_shim() -> None:
        os.kill(int((proj / "shim.pid").read_text()), signal.SIGTERM)

    def _wait_shim_dead(port: int) -> None:
        # プロセス存在判定（os.kill(pid, 0)）はゾンビ（親未 reap）で偽陽性になるため、
        # listen ソケットが閉じて接続拒否になることを終了判定とする。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            except OSError:
                return
            time.sleep(0.05)
        raise AssertionError("shim did not exit")

    def _diag(result) -> str:
        """失敗時診断: 構造化エラーとシムログを丸ごと表示する（CI での原因特定用）。"""
        log_path = cube / "shim.log"
        log = log_path.read_text(errors="replace") if log_path.exists() else "(no shim.log)"
        return f"error={result['error']}\n--- shim.log ---\n{log}"

    # run_untrusted は接続 env（E2B_API_URL 等）を os.environ に直接書くため、
    # live_shim を使う後続テストのために元値を復元する。
    saved = {k: os.environ.get(k) for k in ("E2B_API_URL", "E2B_API_KEY", "SSL_CERT_FILE")}
    try:
        # 初回: シム未稼働（port ファイルすら無い）→ オンデマンド起動 → green。
        r1 = mod.run_untrusted("print(1)", template_id="tmpl-x")
        assert r1["ok"] is True, _diag(r1)
        port1 = int((cube / "port").read_text())

        # アイドル終了相当: シムを止めて port ファイルを stale にする。
        _kill_shim()
        _wait_shim_dead(port1)

        # 再実行: stale port を検出 → 再起動 → 新ポートで green（接続情報の再解決）。
        r2 = mod.run_untrusted("print(2)", template_id="tmpl-x")
        assert r2["ok"] is True, _diag(r2)
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            _kill_shim()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
