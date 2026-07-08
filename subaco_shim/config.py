"""シム設定と .cube ファイルレイアウト、環境変数の参照。

2 種類の設定源を扱う:

1. **シム自身の設定**（エージェント書換不能・リポジトリ外）:
   ``~/.config/subaco-shim/config.toml``。``allow_shared_kernel``（既定 false）と、
   fail-closed 判定用の登録済みリモート接続先レジストリを持つ。
2. **プロジェクト内の .cube レイアウト**:
   ``.cube/port`` / ``.cube/token`` / ``.cube/writer.lock``。ディレクトリ 0700、
   token 0600。``.hive/`` とは別ディレクトリ。

環境変数（`.envrc` がエクスポート・shim が消費）:
``E2B_API_KEY`` / ``E2B_DOMAIN`` / ``CUBE_TEMPLATE_ID`` / ``SUBACO_SHIM_LOG_LEVEL``。

tomllib（stdlib）を使うため外部依存なしで import・動作する。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .isolation import IsolationLevel, fail_closed_remote_level

# --- 環境変数キー -------------------------------------------------


class EnvKeys:
    """`.envrc` がエクスポートし shim / E2B SDK が消費する環境変数名。"""

    E2B_API_KEY = "E2B_API_KEY"  # .cube/token の内容（e2b_<hex32>）
    E2B_DOMAIN = "E2B_DOMAIN"  # 接続先ドメイン（既定はローカル shim）
    E2B_API_URL = "E2B_API_URL"  # 制御プレーン URL（内部用途）
    CUBE_TEMPLATE_ID = "CUBE_TEMPLATE_ID"  # digest 固定 OCI イメージ参照
    SUBACO_SHIM_LOG_LEVEL = "SUBACO_SHIM_LOG_LEVEL"  # 診断ログレベル
    # TODO: E2B_SANDBOX_URL / E2B_DEBUG 等の接続 URL 系の最終形は spike で確定。


def env_str(key: str, default: str | None = None) -> str | None:
    """環境変数を文字列で読む（未設定は default）。"""
    return os.environ.get(key, default)


# --- .cube ファイルレイアウト -------------------------------------

CUBE_DIR_NAME = ".cube"
_PORT_FILE = "port"
_TOKEN_FILE = "token"
_WRITER_LOCK_FILE = "writer.lock"

CUBE_DIR_MODE = 0o700
CUBE_TOKEN_MODE = 0o600


@dataclass(frozen=True)
class CubePaths:
    """プロジェクト内 ``.cube/`` 配下のパス集合。

    - ``root``        : ``.cube/``（0700）
    - ``port``        : ``.cube/port``（シムの listen ポート）
    - ``token``       : ``.cube/token``（E2B_API_KEY 相当、0600）
    - ``writer_lock`` : ``.cube/writer.lock``（単一インスタンス flock。恒久・unlink しない）
    """

    root: Path
    port: Path
    token: Path
    writer_lock: Path

    @classmethod
    def resolve(cls, project_root: Path | str | None = None) -> CubePaths:
        """プロジェクトルート（既定は CWD）から .cube パス集合を構築する。

        `.envrc` / bootstrap が 0700 で初期化する前提だが、shim は自領域が
        無ければ 0700 で作る防御を持つ（:meth:`ensure_dir`）。
        """
        base = Path(project_root) if project_root is not None else Path.cwd()
        root = base / CUBE_DIR_NAME
        return cls(
            root=root,
            port=root / _PORT_FILE,
            token=root / _TOKEN_FILE,
            writer_lock=root / _WRITER_LOCK_FILE,
        )

    def ensure_dir(self) -> None:
        """``.cube/`` を 0700 で用意する（存在時は権限を締め直す）。防御的初期化。"""
        self.root.mkdir(mode=CUBE_DIR_MODE, parents=True, exist_ok=True)
        # 既存ディレクトリの権限を明示的に 0700 へ（umask や既存作成の緩い権限対策）。
        os.chmod(self.root, CUBE_DIR_MODE)


# --- シム設定（config.toml） --------------------------------------------------


@dataclass(frozen=True)
class RemoteEntry:
    """fail-closed 判定用に登録された非 shim 接続先。

    ここに登録された接続先のみ ``microvm-dedicated-kernel`` として扱う。
    """

    domain: str
    kind: str = "cubesandbox"  # "cubesandbox" / "hosted-e2b" 等（記録用ラベル）


def default_config_path() -> Path:
    """``~/.config/subaco-shim/config.toml`` を返す（XDG_CONFIG_HOME を尊重）。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "subaco-shim" / "config.toml"


@dataclass(frozen=True)
class ShimConfig:
    """シム自身の設定（リポジトリ外・エージェント書換不能）。"""

    allow_shared_kernel: bool = False
    remotes: tuple[RemoteEntry, ...] = field(default_factory=tuple)
    source_path: Path | None = None  # 読み取り元（デバッグ用。未存在時は None）

    @classmethod
    def load(cls, path: Path | str | None = None) -> ShimConfig:
        """config.toml を読み取る。ファイル不在時は安全側の既定（default-deny）を返す。

        既定は ``allow_shared_kernel = false`` かつ登録リモートなし。すなわち
        オプトインが無ければ共有カーネルは拒否、未登録リモートは unknown 扱いとなる。
        """
        p = Path(path) if path is not None else default_config_path()
        if not p.is_file():
            return cls(source_path=None)
        with p.open("rb") as fh:
            data = tomllib.load(fh)
        return cls._from_mapping(data, source_path=p)

    @classmethod
    def _from_mapping(cls, data: dict, *, source_path: Path | None) -> ShimConfig:
        allow = bool(data.get("allow_shared_kernel", False))
        remotes: list[RemoteEntry] = []
        # 形式 A: [[remote]] テーブル配列（domain 必須・kind 任意）。
        for entry in data.get("remote", []) or []:
            if isinstance(entry, dict) and entry.get("domain"):
                remotes.append(
                    RemoteEntry(
                        domain=str(entry["domain"]),
                        kind=str(entry.get("kind", "cubesandbox")),
                    )
                )
        # 形式 B: trusted_remotes = ["domain", ...] の簡易リスト。
        for dom in data.get("trusted_remotes", []) or []:
            remotes.append(RemoteEntry(domain=str(dom)))
        return cls(
            allow_shared_kernel=allow,
            remotes=tuple(remotes),
            source_path=source_path,
        )

    def is_registered_remote(self, domain: str | None) -> bool:
        """接続先ドメインが登録済みか（fail-closed 判定の入力）。"""
        if not domain:
            return False
        return any(r.domain == domain for r in self.remotes)

    def isolation_level_for_remote(self, domain: str | None) -> IsolationLevel:
        """非 shim 接続先の隔離レベルを fail-closed で解決する。

        登録済みのみ ``microvm-dedicated-kernel``、未登録は ``unknown``。
        """
        return fail_closed_remote_level(self.is_registered_remote(domain))
