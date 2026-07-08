"""subaco-shim — E2B API 互換ローカル実行シム（cube-shim）。

公開層:

- :mod:`subaco_shim.isolation` — 隔離レベルの序列と default-deny ルーティング
- :mod:`subaco_shim.config`    — シム設定（config.toml）と .cube レイアウト
- :mod:`subaco_shim.tokens`    — .cube/token・.cube/port・単一インスタンス flock
- :mod:`subaco_shim.models`    — E2B 互換 SandboxInfo / Execution 骨子
- :mod:`subaco_shim.drivers`   — ドライバ ABC + podman / mock / apple_container / wslc
- :mod:`subaco_shim.protocol`  — E2B ワイヤプロトコル層の骨子
- :mod:`subaco_shim.server`    — 127.0.0.1 限定 HTTP サーバー・トークン検証・enforce
- :mod:`subaco_shim.lifecycle` — オンデマンド起動・単一インスタンス・アイドル終了
- :mod:`subaco_shim.logging`   — 診断ログ

stdlib のみで全層が import・動作する（podman / e2b SDK は遅延 import）。
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
