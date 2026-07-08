"""models.py の E2B 互換骨子（Execution.text / to_json、SandboxInfo）テスト。"""

from __future__ import annotations

import json

from subaco_shim.isolation import IsolationLevel
from subaco_shim.models import (
    ISOLATION_LEVEL_KEY,
    Execution,
    ExecutionError,
    Logs,
    Result,
    SandboxInfo,
)


def test_execution_text_prefers_main_result():
    exc = Execution(
        results=[
            Result(text="secondary"),
            Result(text="MAIN", is_main_result=True),
        ]
    )
    assert exc.text == "MAIN"


def test_execution_text_falls_back_to_first_text():
    exc = Execution(results=[Result(text="only")])
    assert exc.text == "only"
    assert Execution().text is None


def test_execution_to_json_structure():
    exc = Execution(
        results=[Result(text="hi", is_main_result=True)],
        logs=Logs(stdout=["out"], stderr=["err"]),
        error=ExecutionError(name="ValueError", value="boom"),
        execution_count=1,
    )
    data = json.loads(exc.to_json())
    assert data["text"] == "hi"
    assert data["logs"] == {"stdout": ["out"], "stderr": ["err"]}
    assert data["error"]["name"] == "ValueError"
    assert data["execution_count"] == 1


def test_execution_str_is_repr_not_text():
    # str(Execution) は repr を返す（.text を使うこと）。
    exc = Execution(results=[Result(text="payload", is_main_result=True)])
    assert "payload" not in str(exc) or str(exc).startswith("Execution(")
    assert str(exc).startswith("Execution(")


def test_sandbox_info_isolation_level_from_metadata():
    info = SandboxInfo(
        sandbox_id="sb1",
        template_id="tmpl",
        metadata={ISOLATION_LEVEL_KEY: "vm-per-container"},
    )
    assert info.isolation_level is IsolationLevel.VM_PER_CONTAINER
    # metadata 未設定・未知は unknown（fail-closed）。
    bare = SandboxInfo(sandbox_id="sb2", template_id="tmpl")
    assert bare.isolation_level is IsolationLevel.UNKNOWN


def test_sandbox_info_with_isolation_level_sets_metadata():
    info = SandboxInfo(sandbox_id="sb", template_id="tmpl")
    info.with_isolation_level(IsolationLevel.SHARED_KERNEL)
    assert info.metadata[ISOLATION_LEVEL_KEY] == "shared-kernel"
    assert info.isolation_level is IsolationLevel.SHARED_KERNEL
    # to_json でも隔離レベルが載る。
    assert json.loads(info.to_json())["metadata"][ISOLATION_LEVEL_KEY] == "shared-kernel"
