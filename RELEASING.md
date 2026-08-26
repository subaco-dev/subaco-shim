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
3. `scripts/sandbox_run.py` をこのリリースで変更した場合は、subaco 側で
   `just sync-sandbox-run` を実行してテンプレート正典
   （`templates/multi-agent/scripts/sandbox_run.py`）へ同期する（両コピーは byte 一致が規約。
   `just sync-sandbox-run-check` で検査できる）。
4. subaco 側で smoke CI が green になることを確認してコミットする。

## 備考

- 実行時依存は certifi のみ（`SSL_CERT_FILE` 用の certifi 結合 CA バンドル生成に必須——
  シム証明書単体のバンドルは SDK 側プロセスの通常 HTTPS を壊すため）。requirements には
  certifi が載るのが正常。E2B SDK は test extra で、配布物には含めない（遅延依存方針）。
- `test` extra の E2B SDK は spike 確定の組（`e2b==2.30.0` / `e2b-code-interpreter==2.8.1`）に
  pin 済み。テンプレート側 pyproject（subaco の multi-agent）も同じ組を pin しており、
  SDK を更新する場合はワイヤ契約テスト green を確認してから両方を揃えて上げる。
