"""M2a DoD の自動化部分: Linux + podman の全スタック E2E（実 SDK → シム → 実コンテナ）。

実装計画書 M2a 完了条件のうち「Linux + podman で sandbox_run.py が動作し、実行結果と
隔離レベルが構造化出力として返却され」る部分を自動テスト化する（hive への記録は
手動 E2E シナリオの担当——本モジュールのスコープ外）。

**必ず独立した pytest プロセスで実行する**（``SUBACO_SHIM_LIVE_WIRE=1`` ゲート）:
SDK（httpx）は SSL context をプロセス内でキャッシュするため、test_wire_contract の
mock シムと同一プロセスで走らせると、後から起動した別証明書の本シムを信頼できない
（CERTIFICATE_VERIFY_FAILED）。CI は専用ステップでこのモジュールだけを起動する。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from subaco_shim.config import CubePaths, ShimConfig
from subaco_shim.drivers.podman import PodmanDriver
from subaco_shim.lifecycle import check_subdomain_resolution, start_shim
from subaco_shim.tokens import read_token

e2b_code_interpreter = pytest.importorskip(
    "e2b_code_interpreter", reason="実 SDK 契約テストは uv sync --extra test の環境でのみ実行"
)

TEMPLATE = os.environ.get("SUBACO_SHIM_LIVE_TEMPLATE")

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("SUBACO_SHIM_LIVE_WIRE") != "1",
        reason="SUBACO_SHIM_LIVE_WIRE=1 の専用プロセスでのみ実行（SSL context キャッシュ対策）",
    ),
    pytest.mark.skipif(
        TEMPLATE is None or not PodmanDriver.available(),
        reason="SUBACO_SHIM_LIVE_TEMPLATE 未設定または podman 未検出（実機統合でのみ実行）",
    ),
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
def podman_shim(tmp_path_factory):
    """実 podman ドライバでシムを実起動する（.envrc / M2a-6 と同じ接続配線）。"""
    mp = pytest.MonkeyPatch()
    paths = CubePaths.resolve(tmp_path_factory.mktemp("live-wire"))
    shim = start_shim(
        paths=paths,
        # 実行系 CI はホスト管理者オプトイン済み環境に相当する（M2 DoD の検証前提）。
        # shared-kernel の既定 deny（オプトインなし拒否）自体は test_access_control が検証する。
        config=ShimConfig(allow_shared_kernel=True),
        driver=PodmanDriver(),
        idle_timeout=0,
        default_template_id=TEMPLATE,
    )
    thread = threading.Thread(
        target=shim.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    mp.setenv("E2B_API_URL", f"http://127.0.0.1:{shim.port}")
    mp.setenv("E2B_API_KEY", read_token(paths))
    mp.setenv("SSL_CERT_FILE", str(shim.tls.ca_bundle))
    # sandbox_run に稼働中シムの .cube を明示（CWD 上方探索で別シムを起動させない）。
    mp.setenv("CUBE_DIR", str(paths.root))
    for var in ("E2B_DEBUG", "E2B_SANDBOX_URL", "E2B_DOMAIN"):
        mp.delenv(var, raising=False)
    try:
        yield shim
    finally:
        mp.undo()
        shim.shutdown()
        thread.join(timeout=5)
        shim.close()


def test_sdk_full_stack_on_real_podman(podman_shim):
    """create → run_code → files write/read → get_info → kill を実 SDK × 実コンテナで往復する。"""
    from e2b_code_interpreter import Sandbox

    sbx = Sandbox.create(template=TEMPLATE, metadata={"purpose": "live-wire"})
    try:
        # run_code は実コンテナ内の python3 で評価される（mock のエコーではない）。
        ex = sbx.run_code("print(6 * 7)")
        assert ex.error is None, f"run_code 失敗: {ex.error} logs={ex.logs}"
        assert ex.text is not None and ex.text.strip() == "42"

        # files（envd 面）→ put_file の親ディレクトリ自動作成を実イメージで往復確認。
        sbx.files.write("/work/live.txt", "live subaco")
        assert sbx.files.read("/work/live.txt") == "live subaco"

        # 隔離レベルは get_info の metadata が正典（podman = shared-kernel）。
        info = sbx.get_info()
        assert info.metadata["isolation_level"] == "shared-kernel"
        assert info.metadata["purpose"] == "live-wire"
    finally:
        assert sbx.kill() is True


def test_sandbox_run_structured_output_on_real_podman(podman_shim):
    """M2a DoD: sandbox_run.py が実行結果と隔離レベルを構造化出力として返すこと。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "sandbox_run.py"
    spec = importlib.util.spec_from_file_location("sandbox_run_live_wire", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    result = mod.run_untrusted("print(21 * 2)", template_id=TEMPLATE)
    assert result["ok"] is True, result
    assert result["text"] is not None and result["text"].strip() == "42"
    assert result["isolation_level"] == "shared-kernel"
    assert result["template_id"] == TEMPLATE
    # hive_remember にそのまま渡せる JSON 化可能な構造化出力。
    json.dumps(result)
