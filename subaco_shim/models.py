"""E2B SDK 互換のデータモデル骨子。

`sandbox_run.py` の骨子と整合する最小形:

    with Sandbox.create(template=...) as sb:
        exec = sb.run_code(code)
        return exec.text          # 出力は .text、構造化は .to_json()

- :class:`Execution` は ``.text`` プロパティと ``.to_json()`` を持つ。
  ``str(Execution)`` は repr を返す（str() を使わない）。
- :class:`SandboxInfo` の ``metadata`` には隔離レベルを ``isolation_level``
  キーで載せる（get_info の返却で隔離レベルを伝える正典）。

stdlib のみで動作する（外部依存なし）。E2B SDK 実物の Execution はより多くの
MIME 表現・属性を持つが、v0 骨子ではテキスト経路に必要な最小限に絞る。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .isolation import IsolationLevel, classify

# metadata に隔離レベルを載せる際のキー。全経路で統一する。
ISOLATION_LEVEL_KEY = "isolation_level"


@dataclass
class Logs:
    """run_code の標準出力・標準エラー（E2B ``Logs`` 互換の骨子）。"""

    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {"stdout": list(self.stdout), "stderr": list(self.stderr)}


@dataclass
class ExecutionError:
    """実行時例外情報（E2B ``ExecutionError`` 互換の骨子）。"""

    name: str
    value: str
    traceback: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value, "traceback": self.traceback}


@dataclass
class Result:
    """run_code の 1 結果（E2B ``Result`` 互換の骨子）。

    v0 骨子では text 表現のみを持つ。将来 image/html/json 等の MIME 表現を
    追加する（TODO: カバレッジ確定に合わせて拡張）。
    """

    text: str | None = None
    is_main_result: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "is_main_result": self.is_main_result}


@dataclass
class Execution:
    """run_code の実行結果（E2B ``Execution`` 互換の骨子）。

    ``.text`` は主結果のテキストを返す（E2B の ``Execution.text`` 相当）。
    構造化出力は ``.to_json()`` で取得する。``str()`` は使わない（repr を返す）。
    """

    results: list[Result] = field(default_factory=list)
    logs: Logs = field(default_factory=Logs)
    error: ExecutionError | None = None
    execution_count: int | None = None

    @property
    def text(self) -> str | None:
        """主結果のテキスト表現を返す（E2B ``Execution.text`` 互換）。

        ``is_main_result`` の結果を優先し、なければ最初のテキスト結果を返す。
        テキスト結果が一つも無ければ None。
        """
        for r in self.results:
            if r.is_main_result and r.text is not None:
                return r.text
        for r in self.results:
            if r.text is not None:
                return r.text
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [r.to_dict() for r in self.results],
            "logs": self.logs.to_dict(),
            "error": self.error.to_dict() if self.error else None,
            "execution_count": self.execution_count,
            "text": self.text,
        }

    def to_json(self) -> str:
        """構造化出力を JSON 文字列で返す（E2B ``Execution.to_json`` 互換）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    # __str__ は定義しない → dataclass 既定の __repr__ を使う。
    # str(Execution) は repr を返すため出力取得には .text を使うこと。


@dataclass
class SandboxInfo:
    """サンドボックス情報（E2B ``get_info`` 返却の骨子）。

    ``metadata[ISOLATION_LEVEL_KEY]`` に隔離レベル文字列を必ず載せる
    （隔離レベルの正典な返却経路。``X-Isolation-Level`` ヘッダは
    デバッグ用の補助）。
    """

    sandbox_id: str
    template_id: str
    metadata: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def isolation_level(self) -> IsolationLevel:
        """metadata から隔離レベルを取り出して分類する（未設定・未知は unknown）。"""
        return classify(self.metadata.get(ISOLATION_LEVEL_KEY))

    def with_isolation_level(self, level: IsolationLevel) -> SandboxInfo:
        """隔離レベルを metadata に載せた自身を返す（ドライバの返却整形用）。"""
        self.metadata[ISOLATION_LEVEL_KEY] = str(level)
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "sandbox_id": self.sandbox_id,
            "template_id": self.template_id,
            "metadata": dict(self.metadata),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
