"""scripts/sandbox_run.py の骨子（構造化出力・隔離レベル取得）テスト。

e2b SDK は未インストールでも本テストは動く（sandbox_factory を注入するため）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from subaco_shim.models import Execution, Result

_SANDBOX_RUN = Path(__file__).resolve().parent.parent / "scripts" / "sandbox_run.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sandbox_run", _SANDBOX_RUN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSandbox:
    """e2b Sandbox 互換の最小フェイク（with / run_code / get_info / sandbox_id）。"""

    def __init__(self, *, template, isolation):
        self.template = template
        self._isolation = isolation
        self.sandbox_id = "fake-sbx-1"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run_code(self, code):
        return Execution(results=[Result(text=f"OUT:{code}", is_main_result=True)])

    def get_info(self):
        class _Info:
            metadata = {"isolation_level": self._isolation}

        return _Info()


def test_module_imports_without_e2b():
    # 遅延 import のため e2b 不在でもモジュール自体は import できる。
    mod = _load_module()
    assert hasattr(mod, "run_untrusted")


def test_run_untrusted_structured_output():
    mod = _load_module()

    def factory(*, template):
        return _FakeSandbox(template=template, isolation="vm-per-container")

    result = mod.run_untrusted("print(1)", template_id="tmpl", sandbox_factory=factory)
    assert result["ok"] is True
    assert result["text"] == "OUT:print(1)"
    # 隔離レベルは get_info の metadata から取得。
    assert result["isolation_level"] == "vm-per-container"
    assert result["template_id"] == "tmpl"
    # hive_remember へ渡せる構造化出力（execution は dict 化されている）。
    assert isinstance(result["execution"], dict)


def test_run_untrusted_missing_isolation_is_unknown():
    mod = _load_module()

    def factory(*, template):
        return _FakeSandbox(template=template, isolation=None)

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    # metadata に無ければ fail-closed で unknown。
    assert result["isolation_level"] == "unknown"


def test_run_untrusted_error_path():
    mod = _load_module()

    def factory(*, template):
        raise RuntimeError("boom")

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    assert result["ok"] is False
    assert "boom" in result["error"]


# --- 作成リトライ（接続確立の失敗のみ再試行） --------------------------------


def test_create_retries_on_connection_error():
    mod = _load_module()
    attempts = []

    def factory(*, template):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("shim not up yet")
        return _FakeSandbox(template=template, isolation="vm-per-container")

    result = mod.run_untrusted(
        "x", template_id="t", sandbox_factory=factory, retries=3, retry_wait=0.01
    )
    assert result["ok"] is True
    assert len(attempts) == 3


def test_create_does_not_retry_non_connection_error():
    mod = _load_module()
    attempts = []

    def factory(*, template):
        attempts.append(1)
        raise ValueError("auth-ish failure")  # 非一時的エラーは再試行しない。

    result = mod.run_untrusted(
        "x", template_id="t", sandbox_factory=factory, retries=3, retry_wait=0.01
    )
    assert result["ok"] is False
    assert len(attempts) == 1


def test_create_does_not_retry_read_timeout():
    """ReadTimeout は要求がサーバー側で完了した可能性があるため再試行しない
    （再送はサンドボックスの重複作成＝コンテナ／ネットワークの孤児化を招く）。"""
    httpx = pytest.importorskip("httpx")
    mod = _load_module()
    attempts = []

    def factory(*, template):
        attempts.append(1)
        raise httpx.ReadTimeout("response lost after server processed create")

    result = mod.run_untrusted(
        "x", template_id="t", sandbox_factory=factory, retries=3, retry_wait=0.01
    )
    assert result["ok"] is False
    assert len(attempts) == 1


def test_create_does_not_retry_connection_reset():
    """ConnectionReset も送信後（＝処理された可能性がある）ため再試行しない。"""
    mod = _load_module()
    attempts = []

    def factory(*, template):
        attempts.append(1)
        raise ConnectionResetError("reset mid-flight")

    result = mod.run_untrusted(
        "x", template_id="t", sandbox_factory=factory, retries=3, retry_wait=0.01
    )
    assert result["ok"] is False
    assert len(attempts) == 1


def test_create_retry_exhaustion_returns_error():
    mod = _load_module()
    attempts = []

    def factory(*, template):
        attempts.append(1)
        raise ConnectionRefusedError("never up")

    result = mod.run_untrusted(
        "x", template_id="t", sandbox_factory=factory, retries=2, retry_wait=0.01
    )
    assert result["ok"] is False
    assert "ConnectionRefusedError" in result["error"]
    assert len(attempts) == 3  # 初回 + 再試行 2 回


# --- 実行タイムアウトの伝播 ---------------------------------------------------


def test_timeout_passed_to_run_code():
    mod = _load_module()
    seen = {}

    class _TimeoutSandbox(_FakeSandbox):
        def run_code(self, code, **kwargs):
            seen.update(kwargs)
            return super().run_code(code)

    def factory(*, template):
        return _TimeoutSandbox(template=template, isolation="vm-per-container")

    mod.run_untrusted("x", template_id="t", sandbox_factory=factory, timeout=42.0)
    assert seen == {"timeout": 42.0}
    # timeout 未指定は kwargs ごと省略（SDK 既定 300 秒に委ねる）。
    seen.clear()
    mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    assert seen == {}


# --- post-create フック（ID 取得後・run_code 前） -----------------------------


def test_post_create_hook_runs_between_create_and_run_code():
    mod = _load_module()
    order = []

    class _OrderSandbox(_FakeSandbox):
        def run_code(self, code, **kwargs):
            order.append("run_code")
            return super().run_code(code)

    def factory(*, template):
        order.append("create")
        return _OrderSandbox(template=template, isolation="vm-per-container")

    def hook(sb):
        order.append("hook")
        assert sb.sandbox_id  # ID 取得済みの段階で呼ばれる。

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory, post_create_hook=hook)
    assert result["ok"] is True
    assert order == ["create", "hook", "run_code"]


def test_post_create_cmd_invoked_with_sandbox_id(monkeypatch, tmp_path):
    mod = _load_module()
    out = tmp_path / "hook.out"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\necho "$1" > {out}\n')
    script.chmod(0o755)
    monkeypatch.setenv("CUBE_POST_CREATE_CMD", str(script))

    def factory(*, template):
        return _FakeSandbox(template=template, isolation="vm-per-container")

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    assert result["ok"] is True
    assert out.read_text().strip() == "fake-sbx-1"


def test_post_create_cmd_failure_aborts_run(monkeypatch, tmp_path):
    mod = _load_module()
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nexit 7\n")
    script.chmod(0o755)
    monkeypatch.setenv("CUBE_POST_CREATE_CMD", str(script))
    ran = []

    class _NoRunSandbox(_FakeSandbox):
        def run_code(self, code, **kwargs):
            ran.append(code)
            return super().run_code(code)

    def factory(*, template):
        return _NoRunSandbox(template=template, isolation="vm-per-container")

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    # フック失敗（非ゼロ終了）は実行中断（名前解決の準備ができていない状態で走らせない）。
    assert result["ok"] is False
    assert ran == []


# --- 非 shim 接続先の fail-closed 判定（設計書 §5.3） --------------------------


def test_remote_domain_registered_is_microvm(monkeypatch, tmp_path):
    mod = _load_module()
    config_dir = tmp_path / "subaco-shim"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'trusted_remotes = ["sandbox.example.com"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("E2B_DOMAIN", "sandbox.example.com")

    def factory(*, template):
        return _FakeSandbox(template=template, isolation=None)  # metadata に隔離レベルなし

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    # 登録済みリモートのみ microvm-dedicated-kernel。
    assert result["isolation_level"] == "microvm-dedicated-kernel"


def test_remote_domain_unregistered_stays_unknown(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # 設定ファイル不在
    monkeypatch.setenv("E2B_DOMAIN", "unregistered.example.com")

    def factory(*, template):
        return _FakeSandbox(template=template, isolation=None)

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    assert result["isolation_level"] == "unknown"


def test_remote_domain_does_not_override_metadata(monkeypatch, tmp_path):
    # metadata に隔離レベルがあればそれが正典（リモート判定は unknown 時のみ）。
    mod = _load_module()
    config_dir = tmp_path / "subaco-shim"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'trusted_remotes = ["sandbox.example.com"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("E2B_DOMAIN", "sandbox.example.com")

    def factory(*, template):
        return _FakeSandbox(template=template, isolation="shared-kernel")

    result = mod.run_untrusted("x", template_id="t", sandbox_factory=factory)
    assert result["isolation_level"] == "shared-kernel"
