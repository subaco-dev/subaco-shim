"""ドライバ抽象の共通契約テスト。

このスイートは「3 ドライバ（podman / container / wslc）共通のドライバ抽象契約」を 1 か所で
表現し、それを **mock ドライバに適用**して緑にする（実コンテナ・外部依存なし）。契約の核は:

- ``create → exec → put_file → get_file → get_info → destroy`` のライフサイクルが成立すること。
- **隔離レベルの 3 値保証**: ローカル create の隔離レベルは必ず 3 値のいずれか
  （``unknown`` 禁止）で、``get_info`` の ``metadata[isolation_level]`` が正典として一致すること。
- ファイル入出力がバイト完全一致で往復すること（envd files 相当。ホストマウント非依存）。
- destroy でサンドボックス個別ネットワークの残骸が残らないこと。

同一契約は実ドライバ（podman 等）へも再利用できるよう ``assert_driver_contract`` に切り出す。
実バックエンドでの e2e は実機統合でのみ走らせる opt-in テストとして下部に置く
（``SUBACO_SHIM_LIVE_TEMPLATE`` 未設定なら skip）。
"""

from __future__ import annotations

import os

import pytest

from subaco_shim.drivers import (
    AppleContainerDriver,
    Driver,
    MockDriver,
    PodmanDriver,
    WslcDriver,
    build_driver,
)
from subaco_shim.isolation import IsolationLevel
from subaco_shim.models import ISOLATION_LEVEL_KEY, Execution, SandboxInfo

# 具体ドライバが宣言してよい隔離レベル（unknown を含まない 3 値）。
_THREE_LEVELS = frozenset(
    {
        IsolationLevel.MICROVM_DEDICATED_KERNEL,
        IsolationLevel.VM_PER_CONTAINER,
        IsolationLevel.SHARED_KERNEL,
    }
)

# 6 つの抽象インターフェース（get_info を含む）。
_INTERFACE = ("create", "exec", "put_file", "get_file", "destroy", "get_info")

# レジストリに載る具体ドライバクラス（3 バックエンド + 契約テスト用 mock）。
_CONCRETE_DRIVER_CLASSES = (MockDriver, PodmanDriver, AppleContainerDriver, WslcDriver)


def assert_driver_contract(
    driver: Driver,
    *,
    template: str = "tmpl@sha256:contract",
    strict_destroy: bool = True,
) -> None:
    """ドライバ抽象の共通契約を 1 ライフサイクル分だけ検証する（3 ドライバ共通）。

    引数:
        driver: 契約を満たすべきドライバ実装（mock でも実ドライバでも可）。
        template: create に渡す OCI テンプレート参照。
        strict_destroy: True なら destroy 後に当該サンドボックス操作が失敗する（mock 等の
            厳密破棄）ことも検証する。podman ドライバは get_info が宣言隔離レベルの
            フォールバックを返す設計のため、実ドライバでは False を渡す。
    """
    # --- create: SandboxInfo と隔離レベルの正典（metadata）を検証 ------
    info = driver.create(template_id=template, metadata={"origin": "contract"})
    assert isinstance(info, SandboxInfo)
    sid = info.sandbox_id
    assert sid, "create は非空の sandbox_id を返すこと"
    assert info.template_id == template
    # 3 値保証: 宣言隔離レベルがそのまま載り、unknown ではない。
    assert info.isolation_level is driver.isolation_level
    assert info.isolation_level in _THREE_LEVELS
    assert info.isolation_level is not IsolationLevel.UNKNOWN
    assert info.metadata[ISOLATION_LEVEL_KEY] == str(driver.isolation_level)
    # 呼び出し側 metadata は保持される。
    assert info.metadata.get("origin") == "contract"

    # --- exec: Execution を返し .text で主結果を取れる（str() は使わない） --------
    ex = driver.exec(sid, "print(1)")
    assert isinstance(ex, Execution)
    assert ex.text is not None

    # --- put/get: envd files 相当のバイト完全一致往復（ホストマウント非依存） ------
    payload = b"contract-bytes-\x00\x01\xfe\xff"
    driver.put_file(sid, "/work/contract.bin", payload)
    assert driver.get_file(sid, "/work/contract.bin") == payload

    # --- get_info: 隔離レベルは metadata が正典（X-Isolation-Level は補助） --------
    gi = driver.get_info(sid)
    assert gi.isolation_level is driver.isolation_level
    assert gi.metadata[ISOLATION_LEVEL_KEY] == str(driver.isolation_level)

    # --- destroy: 例外なく破棄し、個別ネットワーク残骸を残さない ----------------
    driver.destroy(sid)
    live = getattr(driver, "live_networks", None)
    if live is not None:
        assert not live, "destroy 後に生存ネットワークが残らないこと（リークなし）"
    if strict_destroy:
        # 破棄済みサンドボックスへの操作は失敗する（mock は KeyError 系）。
        with pytest.raises(KeyError):
            driver.get_info(sid)


# --- mock ドライバへ契約スイートを適用（CI の主役・常時 green） ---------------------


@pytest.mark.parametrize(
    "make_driver",
    [
        pytest.param(lambda: MockDriver(), id="mock-shared-kernel"),
        pytest.param(
            lambda: MockDriver(isolation_level=IsolationLevel.VM_PER_CONTAINER),
            id="mock-vm-per-container",
        ),
        pytest.param(
            lambda: MockDriver(isolation_level=IsolationLevel.MICROVM_DEDICATED_KERNEL),
            id="mock-microvm",
        ),
    ],
)
def test_driver_contract_on_mock(make_driver):
    # 同一契約スイートを、宣言隔離レベルを変えた mock ドライバに適用する。
    assert_driver_contract(make_driver())


# --- 3 ドライバ共通の抽象契約（バックエンド不在でも成立する静的性質） ----------------


@pytest.mark.parametrize("cls", _CONCRETE_DRIVER_CLASSES, ids=lambda c: c.__name__)
def test_shared_abstraction_contract(cls):
    """全具体ドライバが満たすべき抽象契約（型・隔離レベル・可用性判定）。"""
    assert issubclass(cls, Driver)
    # 抽象メソッドを全実装済み（= インスタンス化可能な具体クラス）。
    assert cls.__abstractmethods__ == frozenset()
    # バックエンド CLI が無くても import・インスタンス化はできる（遅延検出）。
    driver = cls()
    # 隔離レベルは 3 値のいずれか（unknown 禁止）。
    assert driver.isolation_level in _THREE_LEVELS
    assert driver.isolation_level is not IsolationLevel.UNKNOWN
    # name は非空の識別子（base のまま出荷しない）。
    assert isinstance(cls.name, str)
    assert cls.name not in ("", "base")
    # available() は bool を返す classmethod（ホスト検出の可否）。
    assert isinstance(cls.available(), bool)
    # 6 つの抽象インターフェースを備える。
    for meth in _INTERFACE:
        assert callable(getattr(driver, meth))


def test_expected_isolation_levels_per_backend():
    # バックエンドと隔離レベルの対応。
    assert PodmanDriver.isolation_level is IsolationLevel.SHARED_KERNEL
    assert AppleContainerDriver.isolation_level is IsolationLevel.VM_PER_CONTAINER
    assert WslcDriver.isolation_level is IsolationLevel.SHARED_KERNEL
    # wslc は GA 前は experimental フラグ付き。
    assert WslcDriver.experimental is True


# --- 実バックエンドでの e2e 契約（実機統合でのみ・既定は skip） -----------------


@pytest.mark.parametrize("driver_name", ["podman", "container", "wslc"])
def test_real_driver_contract_opt_in(driver_name):
    """同一契約スイートを実ドライバへ再利用する opt-in 経路（実機統合）。

    ``SUBACO_SHIM_LIVE_TEMPLATE`` に provision 済み OCI テンプレート参照を渡したときのみ走る。
    未設定・バックエンド未検出では skip（このマシンや通常 CI では常に skip）。
    """
    template = os.environ.get("SUBACO_SHIM_LIVE_TEMPLATE")
    if template is None:
        pytest.skip("SUBACO_SHIM_LIVE_TEMPLATE 未設定（実機統合でのみ実行）")
    driver = build_driver(driver_name)
    if not driver.available():
        pytest.skip(f"{driver_name}: バックエンド未検出（このホストでは対象外）")
    # 実ドライバは get_info がフォールバックを返し得るため strict_destroy=False。
    assert_driver_contract(driver, template=template, strict_destroy=False)
