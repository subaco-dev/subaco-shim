"""ドライバ層。

抽象基底 :class:`~subaco_shim.drivers.base.Driver` と、その具体実装:

- :class:`~subaco_shim.drivers.podman.PodmanDriver`        — podman（shared-kernel）
- :class:`~subaco_shim.drivers.mock.MockDriver`            — in-memory 契約テスト用（CI の主役）
- :class:`~subaco_shim.drivers.apple_container.AppleContainerDriver`
      — Apple Container（vm-per-container、experimental スケルトン）
- :class:`~subaco_shim.drivers.wslc.WslcDriver` — wslc（shared-kernel、experimental）

各具体ドライバはバックエンド CLI をサブプロセスで扱い、CLI 不在でもモジュール自体は
import 可能に保つ。ドライバ選択は :func:`build_driver` / :func:`select_driver`。
"""

from __future__ import annotations

from .apple_container import AppleContainerDriver
from .base import Driver
from .mock import MockDriver
from .podman import PodmanDriver
from .wslc import WslcDriver

__all__ = [
    "AppleContainerDriver",
    "Driver",
    "MockDriver",
    "PodmanDriver",
    "WslcDriver",
    "build_driver",
    "select_driver",
]

# 名前 → ドライバクラス（CLI ``--driver`` と build_driver が参照）。
_REGISTRY: dict[str, type[Driver]] = {
    "podman": PodmanDriver,
    "mock": MockDriver,
    "container": AppleContainerDriver,
    "wslc": WslcDriver,
}

# auto 検出でローカル実行に採る優先順（強隔離 → 弱隔離）。
# Apple Container（vm-per-container）を最優先、次に podman（shared-kernel）、次に wslc。
_AUTO_ORDER: tuple[str, ...] = ("container", "podman", "wslc")


def build_driver(name: str) -> Driver:
    """名前からドライバインスタンスを生成する（``auto`` は :func:`select_driver`）。"""
    if name == "auto":
        return select_driver()
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"未知のドライバ: {name!r}（候補: {sorted(_REGISTRY)}）") from exc
    return cls()


def select_driver() -> Driver:
    """このホストで利用可能なドライバを優先順で 1 つ選ぶ。

    :attr:`Driver.available` が True の最初のドライバを返す。いずれも利用不可なら
    :class:`RuntimeError`（呼び出し側は runbook を案内する）。契約テスト／dev では
    ``build_driver("mock")`` を明示指定する。
    """
    for name in _AUTO_ORDER:
        cls = _REGISTRY[name]
        if cls.available():
            return cls()
    raise RuntimeError(
        "利用可能な実行バックエンドがありません（podman / Apple Container / wslc を未検出）。"
        " 契約テスト・dev では --driver mock を指定してください。"
    )
