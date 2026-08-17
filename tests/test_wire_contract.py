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
