"""isolation.py の序列・ルーティング・fail-closed 判定のテスト。"""

from __future__ import annotations

from subaco_shim.isolation import (
    REASON_SHARED_KERNEL_DENIED,
    REASON_SHARED_KERNEL_OPT_IN,
    REASON_UNKNOWN_DENIED,
    REASON_VM_OR_STRONGER,
    IsolationLevel,
    classify,
    fail_closed_remote_level,
    is_stronger_or_equal,
    rank,
    route_execution,
)


def test_ordering():
    # microvm > vm-per-container > shared-kernel > unknown（記録専用・最下位）
    assert rank(IsolationLevel.MICROVM_DEDICATED_KERNEL) > rank(IsolationLevel.VM_PER_CONTAINER)
    assert rank(IsolationLevel.VM_PER_CONTAINER) > rank(IsolationLevel.SHARED_KERNEL)
    assert rank(IsolationLevel.SHARED_KERNEL) > rank(IsolationLevel.UNKNOWN)
    assert is_stronger_or_equal(IsolationLevel.VM_PER_CONTAINER, IsolationLevel.SHARED_KERNEL)
    assert not is_stronger_or_equal(IsolationLevel.SHARED_KERNEL, IsolationLevel.VM_PER_CONTAINER)


def test_classify_unknown_fallback():
    assert classify("microvm-dedicated-kernel") is IsolationLevel.MICROVM_DEDICATED_KERNEL
    assert classify("nonsense") is IsolationLevel.UNKNOWN
    assert classify(None) is IsolationLevel.UNKNOWN


def test_route_vm_or_stronger_always_allowed():
    for lvl in (
        IsolationLevel.MICROVM_DEDICATED_KERNEL,
        IsolationLevel.VM_PER_CONTAINER,
    ):
        # オプトインの有無にかかわらず許可される。
        for opt in (True, False):
            d = route_execution(lvl, allow_shared_kernel=opt)
            assert d.allowed is True
            assert d.reason == REASON_VM_OR_STRONGER


def test_route_shared_kernel_requires_opt_in():
    denied = route_execution(IsolationLevel.SHARED_KERNEL, allow_shared_kernel=False)
    assert denied.allowed is False
    assert denied.reason == REASON_SHARED_KERNEL_DENIED

    allowed = route_execution(IsolationLevel.SHARED_KERNEL, allow_shared_kernel=True)
    assert allowed.allowed is True
    assert allowed.reason == REASON_SHARED_KERNEL_OPT_IN


def test_route_unknown_always_denied():
    # unknown は allow_shared_kernel の有無にかかわらず無条件拒否。
    for opt in (True, False):
        d = route_execution(IsolationLevel.UNKNOWN, allow_shared_kernel=opt)
        assert d.allowed is False
        assert d.reason == REASON_UNKNOWN_DENIED


def test_fail_closed_remote_level():
    # 登録済みのみ microvm、未登録は unknown。
    assert fail_closed_remote_level(True) is IsolationLevel.MICROVM_DEDICATED_KERNEL
    assert fail_closed_remote_level(False) is IsolationLevel.UNKNOWN


def test_route_signature_has_no_trust_param():
    # trust でルーティングを緩和しない設計を回帰的に守る。
    import inspect

    params = inspect.signature(route_execution).parameters
    assert "trust" not in params
    assert "level" in params and "allow_shared_kernel" in params
