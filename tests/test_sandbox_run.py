"""scripts/sandbox_run.py の骨子（構造化出力・隔離レベル取得）テスト。

e2b SDK は未インストールでも本テストは動く（sandbox_factory を注入するため）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from subaco_shim.models import Execution, Result

_SANDBOX_RUN = Path(__file__).resolve().parent.parent / "scripts" / "sandbox_run.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sandbox_run", _SANDBOX_RUN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSandbox:
    """e2b Sandbox 互換の最小フェイク（with / run_code / get_info）。"""

    def __init__(self, *, template, isolation):
        self.template = template
        self._isolation = isolation

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
