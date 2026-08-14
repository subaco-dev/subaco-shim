# subaco-shim タスクランナー（just 規約: test / lint / fmt）
# エージェントは AGENTS.md から `just test` 等を参照する。コマンド探索のムダを消す。

# 既定: 一覧を表示
default:
    @just --list

# 依存を同期（テスト用 extra を含む。オフライン時は失敗し得る）
sync:
    uv sync --extra test --extra dev

# テスト（stdlib のみで動く基盤層は外部依存なしで green）
test:
    uv run --with pytest pytest -q

# lint（ruff）
lint:
    uv run --with ruff ruff check .

# format（ruff format）
fmt:
    uv run --with ruff ruff format .

# format チェック（CI 用・非破壊）
fmt-check:
    uv run --with ruff ruff format --check .

# 構文チェックのみ（重い依存なしのオフライン確認）
compile:
    python3 -m py_compile subaco_shim/*.py subaco_shim/drivers/*.py subaco_shim/protocol/*.py scripts/*.py

# CI 相当をローカルで再現（lint + 整形チェック + test。.github/workflows/ci.yml と対応）
ci: lint fmt-check test

# リリース用: テンプレート同梱の固定 requirements を生成（RELEASING.md「公開後」参照）。
# プロジェクト自身は含めない（起動スクリプトが `uvx ... subaco-shim==<版>` で版を固定する）。
export-reqs:
    uv export --no-dev --no-emit-project --format requirements-txt -o requirements-shim.txt
