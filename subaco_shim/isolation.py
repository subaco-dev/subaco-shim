"""隔離レベルの序列と実行先ルーティング。

このモジュールは stdlib のみに依存し、外部依存なしで import・動作する。

隔離レベルは 3 値 + `unknown`（記録専用・最下位）で表す:

    microvm-dedicated-kernel  > vm-per-container > shared-kernel  > unknown
    （CubeSandbox / E2B）        （Apple Container）  （podman/wslc）   （記録専用）

ルーティングは **default-deny** を正とする:

- 全実行要求は未信頼として扱う。
- vm-per-container 以上は無条件に実行可。
- shared-kernel はホスト管理者の明示オプトイン
  （`~/.config/subaco-shim/config.toml` の ``allow_shared_kernel = true``）が
  ある場合のみ実行可。
- unknown は ``allow_shared_kernel`` の有無にかかわらず**無条件に拒否**する。
- **エージェント申告の trust 値ではこの規則を緩和しない**。そのため
  :func:`route_execution` は意図的に trust 引数を受け取らない。

fail-closed 判定: 非 shim 接続先（リモート）の隔離レベルは、
ホスト管理者設定に登録済みの接続先のときのみ ``microvm-dedicated-kernel`` とし、
未登録は ``unknown``（序列最下位）として扱う。ローカル create では ``unknown`` は
生じない（ドライバは必ず 3 値のいずれかを返すことを保証する）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IsolationLevel(StrEnum):
    """隔離強度の 3 値 + unknown（記録専用・最下位）。

    ``StrEnum``（3.11+）のため値はそのまま str であり、metadata / ヘッダの文字列表現に
    使える（``str(level)`` は値を返す）。
    """

    MICROVM_DEDICATED_KERNEL = "microvm-dedicated-kernel"  # CubeSandbox / ホステッド E2B
    VM_PER_CONTAINER = "vm-per-container"  # Apple Container
    SHARED_KERNEL = "shared-kernel"  # podman / wslc
    UNKNOWN = "unknown"  # 接続先未登録のリモート記録時のみ。序列最下位


# 序列（rank が大きいほど強い隔離）。unknown は shared-kernel 未満の最下位。
_RANK: dict[IsolationLevel, int] = {
    IsolationLevel.MICROVM_DEDICATED_KERNEL: 3,
    IsolationLevel.VM_PER_CONTAINER: 2,
    IsolationLevel.SHARED_KERNEL: 1,
    IsolationLevel.UNKNOWN: 0,
}


def rank(level: IsolationLevel) -> int:
    """隔離レベルの序列値を返す（大きいほど強い）。"""
    return _RANK[level]


def is_stronger_or_equal(a: IsolationLevel, b: IsolationLevel) -> bool:
    """``a`` の隔離が ``b`` 以上（同等含む）かを返す。"""
    return _RANK[a] >= _RANK[b]


def classify(raw: str | None) -> IsolationLevel:
    """任意の文字列を隔離レベルへ分類する。未知の値・None は ``unknown``。

    非 shim バックエンドの metadata 由来ラベルや、外部から受け取った値を
    安全に取り込むためのフェイルセーフ変換（fail-closed）。
    """
    if raw is None:
        return IsolationLevel.UNKNOWN
    try:
        return IsolationLevel(raw)
    except ValueError:
        return IsolationLevel.UNKNOWN


def fail_closed_remote_level(is_registered: bool) -> IsolationLevel:
    """非 shim 接続先（リモート）の隔離レベルを fail-closed で判定する。

    登録済み接続先のみ ``microvm-dedicated-kernel``、
    未登録は ``unknown``（記録専用・最下位）。接続先が登録済みかの判定は
    :class:`subaco_shim.config.ShimConfig` が保持する登録レジストリで行う。
    """
    return IsolationLevel.MICROVM_DEDICATED_KERNEL if is_registered else IsolationLevel.UNKNOWN


# ルーティング判定理由コード（英語・安定文字列。ログ／HTTP エラー本文に載せる）
REASON_VM_OR_STRONGER = "allowed:vm-or-stronger"
REASON_SHARED_KERNEL_OPT_IN = "allowed:shared-kernel-opt-in"
REASON_SHARED_KERNEL_DENIED = "denied:shared-kernel-not-opted-in"
REASON_UNKNOWN_DENIED = "denied:unknown-isolation"


@dataclass(frozen=True)
class RoutingDecision:
    """ルーティング判定結果（許可可否と理由コード）。"""

    allowed: bool
    level: IsolationLevel
    reason: str  # 上記 REASON_* のいずれか


def route_execution(
    level: IsolationLevel,
    *,
    allow_shared_kernel: bool,
) -> RoutingDecision:
    """default-deny ルーティング規則で実行可否を判定する。

    引数:
        level: 実行先バックエンドの隔離レベル（ローカルは必ず 3 値のいずれか）。
        allow_shared_kernel: ホスト管理者のオプトイン（config.toml 由来）。

    注意:
        本関数は **エージェント申告の trust 値を受け取らない**。プロンプト
        インジェクションに乗っ取られたエージェントの自己申告は信頼できないため、
        trust でルーティングを緩和してはならない。この非対応は
        仕様であり、将来も引数追加で緩和経路を作らないこと。
    """
    if is_stronger_or_equal(level, IsolationLevel.VM_PER_CONTAINER):
        # vm-per-container 以上（Apple Container / microvm）は無条件に実行可。
        return RoutingDecision(True, level, REASON_VM_OR_STRONGER)

    if level is IsolationLevel.SHARED_KERNEL:
        # 共有カーネルは明示オプトイン時のみ許可。
        if allow_shared_kernel:
            return RoutingDecision(True, level, REASON_SHARED_KERNEL_OPT_IN)
        return RoutingDecision(False, level, REASON_SHARED_KERNEL_DENIED)

    # unknown（およびそれ未満）はオプトインの有無にかかわらず無条件拒否。
    return RoutingDecision(False, level, REASON_UNKNOWN_DENIED)
