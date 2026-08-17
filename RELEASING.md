# subaco-shim リリース手順（M2b-3(c)）

semver タグ push で `.github/workflows/release.yml` がビルド・検証・PyPI 公開（Trusted
Publishing）・固定 requirements 生成までを行う。人手の手順は以下のみ。

## 初回のみ（リポジトリ・PyPI の下準備）

1. GitHub リポジトリを作成して push する（`gh repo create subaco-dev/subaco-shim --public`）。
2. PyPI で **パッケージ名 `subaco-shim` の可用性を確認・確保**する（実装計画書 M0-1 の残作業。
   初回公開が名前確保を兼ねる）。
3. PyPI → Account settings → Publishing → **pending publisher** を登録:
   - PyPI Project Name: `subaco-shim`
   - Owner / Repository: `subaco-dev` / `subaco-shim`
   - Workflow name: `release.yml`
   - Environment: `pypi`
4. GitHub リポジトリ → Settings → Environments → `pypi` を作成
   （必要なら protection rules で承認者を設定）。

## 毎リリース

1. バージョンを 2 箇所同時に上げる（release.yml が一致をゲートする）:
   - `pyproject.toml` の `[project] version`
   - `subaco_shim/_version.py` の `__version__`
2. コミットして semver タグを push:

   ```sh
   git commit -am "release: v0.1.0"
   git tag v0.1.0
   git push origin main v0.1.0
   ```

3. Release ワークフローの green を確認（バージョンゲート → テスト → build → publish）。

## 公開後（subaco テンプレートへの反映）

1. Release 添付（artifact `dist`）の `requirements-shim.txt` を取得し、subaco リポジトリの
   `templates/multi-agent/requirements-shim.txt` を置き換える
   （ローカル生成する場合: `just export-reqs`）。
2. `templates/multi-agent/wrappers/cube-shim.sh` の固定版（`subaco-shim==<版>`）を更新する。
3. subaco 側で smoke CI が green になることを確認してコミットする。

## 備考

- 実行時依存は certifi のみ（`SSL_CERT_FILE` 用の certifi 結合 CA バンドル生成に必須——
  シム証明書単体のバンドルは SDK 側プロセスの通常 HTTPS を壊すため）。requirements には
  certifi が載るのが正常。E2B SDK は test extra で、配布物には含めない（遅延依存方針）。
- E2B SDK 互換の再現範囲が確定する M2a-1 spike の結果次第で、`test` extra の
  `e2b-code-interpreter` を固定版に pin する（pyproject.toml の TODO）。
