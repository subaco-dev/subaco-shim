# subaco-shim（cube-shim）

E2B API 互換のローカル実行シム。エージェント生成コードを OS ネイティブのコンテナ機構
（podman / Apple Container / wslc）で隔離実行する、Subaco の**実行プレーン**。

- 論理名: cube-shim / 配布名: `subaco-shim` / パッケージ: `subaco_shim`
- 言語: Python 3.11+ / ライセンス: Apache-2.0
- エントリポイント: `subaco-shim = subaco_shim.cli:main`（`subaco-shim serve` で起動）

## E2B ワイヤ互換（5 系統・SDK 無改造）

固定バージョンの実 SDK（`e2b==2.30.0` / `e2b-code-interpreter==2.8.1`）が**無改造・root 不要・
外部 DNS 不要**で動作するワイヤ面を提供する（実測仕様は
`docs/00-memo/05_spike結果_E2B_ワイヤ.md`、判定 full-fidelity-feasible）。

- **制御プレーン**（平文 HTTP・`E2B_API_URL=http://127.0.0.1:{.cube/port}`・`X-API-KEY`）:
  `POST /sandboxes`(201)・`GET /sandboxes/{id}`(200)・`DELETE /sandboxes/{id}`(204/404)。
  エラーは `{"code", "message"}` 形。
- **データプレーン**（単一 TLS リスナー・`X-Access-Token`）: create 応答の
  `domain: "sbx.localhost:{TLS ポート}"` により、SDK の全データプレーン URL が
  `https://{port}-{sandbox_id}.sbx.localhost:{TLS ポート}` に集約される。Host ヘッダの
  `{port}-{sandbox_id}` で多重化し、`49983` 面は `GET/POST /files`・`GET /health`、
  `49999` 面は `POST /execute`（chunked HTTP/1.1・改行区切り JSON ストリーム）を受ける。
- TLS は `.cube/tls/` の `*.sbx.localhost` ワイルドカード自己署名証明書（初回生成・再利用・
  ALPN h2 非広告）。SDK 側は `SSL_CERT_FILE={.cube/tls/ca-bundle.pem}`（certifi 結合バンドル）で
  検証する。`E2B_DEBUG` / `E2B_SANDBOX_URL` は不使用（spike で方式不成立を実測）。

| 操作 | プレーン | 認証ヘッダ | ワイヤ |
|---|---|---|---|
| create | 制御 | `X-API-KEY` | `POST /sandboxes` → 201 + `{sandboxID, clientID, templateID, envdVersion, envdAccessToken, domain}` |
| get_info | 制御 | `X-API-KEY` | `GET /sandboxes/{id}` → 200 + SandboxDetail（必須 10 キー + metadata） |
| destroy | 制御 | `X-API-KEY` | `DELETE /sandboxes/{id}` → 204（`kill()==True`）/ 404（False） |
| run_code | データ | `X-Access-Token` | 49999 面 `POST /execute` → chunked JSON lines |
| files write | データ | `X-Access-Token` | 49983 面 `POST /files?path=`（multipart）→ 非空 JSON 配列 |
| files read | データ | `X-Access-Token` | 49983 面 `GET /files?path=` → 生バイト |

- create 応答の `envdAccessToken` を SDK が以後の全データプレーン要求に `X-Access-Token` として
  自動付与する（サンドボックスごとに一意発行 = 多重化キーを兼ねる）。
- なし／不一致は**両プレーンとも 401**（`tests/test_access_control.py` が実ソケットで検証）。
- 実 SDK をクライアントに使うワイヤ契約 E2E は `tests/test_wire_contract.py`（`just test-wire`。
  CI の wire-contract ジョブが ubuntu + macos で実行）。

## 隔離モデルと default-deny

```
microvm-dedicated-kernel > vm-per-container > shared-kernel > unknown（記録専用・最下位）
```

全実行要求を未信頼として扱い、**default-deny** で判定する。

- **vm-per-container 以上**（Apple Container / microvm）: オプトイン不要で無条件に実行可（201）。
- **shared-kernel**（podman / wslc）: ホスト管理者の明示オプトイン（後述 `allow_shared_kernel`）が
  ある場合のみ実行可。未オプトインは **403**（`reason=denied:shared-kernel-not-opted-in`）。
- **unknown**: `allow_shared_kernel` の有無にかかわらず**無条件拒否**（403）。
- 隔離レベルの正典は `get_info` の `metadata["isolation_level"]`。`X-Isolation-Level` は
  デバッグ用の補助ヘッダ。

**エージェント申告の trust では緩和しない。** プロンプトインジェクションに乗っ取られた
エージェントの自己申告は信頼できないため、判定関数 `route_execution(level, *, allow_shared_kernel)`
は意図的に trust 引数を持たない。この非対応は仕様であり、将来も緩和経路を足さない
（`tests/test_isolation.py` / `tests/test_server.py` が回帰ガード）。

### allow_shared_kernel オプトイン（リポジトリ外・エージェント書換不能）

shared-kernel の許可はホスト管理者のみが行う。エージェントが触れられない
`~/.config/subaco-shim/config.toml` に置く:

```toml
# 共有カーネル層（podman / wslc）での実行を許可する（既定 false）。
allow_shared_kernel = true

# fail-closed 判定用の登録済みリモート接続先（登録済みのみ microvm-dedicated-kernel 扱い）。
# [[remote]]
# domain = "sandbox.example.com"
# kind   = "cubesandbox"
```

ファイル不在時は安全側の既定（`allow_shared_kernel = false`・登録リモートなし）で動く。

## ネットワーク分離（サンドボックス間の相互遮断）

未信頼コードは **egress を持たない内部ネットワーク**（`podman network create --internal
cube-<id>`）で実行する。`--network=none` は使わない（ホスト → データプレーンの TCP 到達性を
維持するため）。ネットワークは**サンドボックスごとに個別作成**してサンドボックス間の相互到達を
遮断し、**destroy 時に当該ネットワークの残骸のみを掃除**する。ホスト
ディレクトリのマウントは禁止（`-v` を一切付けない）。ファイル入出力は `put_file` / `get_file`
（`podman exec` の stdin/stdout）に限定する。`tests/test_network_isolation.py` がこの意図を
コマンド列レベルで固定する。

## ポート動的割当と .cube レイアウト

シムは 127.0.0.1 の**動的ポート**（`0` 指定で OS が付与）に bind し、実ポートを `.cube/port` に
書き出す。トークンは初回のみ生成して `.cube/token` に 0600 で永続・**再起動で再利用**する。
`.envrc` / `sandbox_run.py` は呼び出し時に `.cube/port` / `.cube/token` を**読み直す**ため、
アイドル終了・再起動でポートが変わっても接続が回復する。

```
<project>/.cube/          # 0700, gitignore（.hive とは別ディレクトリ）
├── port                  # 制御プレーンの listen ポート（動的割当を書き出す）
├── token                 # E2B_API_KEY 相当（e2b_<hex32>, 0600, 再起動で再利用）
├── writer.lock           # 単一インスタンス flock（恒久・unlink しない）
└── tls/                  # データプレーン TLS 資材（0700。初回生成・再利用）
    ├── cert.pem          # *.sbx.localhost ワイルドカード自己署名証明書
    ├── key.pem           # 秘密鍵（0600）
    └── ca-bundle.pem     # certifi + cert の結合バンドル（SSL_CERT_FILE 用）
```

データプレーン TLS の実ポートはファイルには書かず、create 応答の `domain` に埋め込んで
SDK へ伝える。単一インスタンスは `.cube/writer.lock` の `flock`（プロジェクト単位に
1 プロセス）で保証する。無操作が閾値（既定 300 秒）を超えると自動終了する
（`--idle-timeout` で調整）。

### `.envrc` 配線（テンプレート側・M2b-3）

```bash
# いずれもファイル存在時のみ export し、呼び出し側は実行時に読み直す。
export E2B_API_KEY="$(cat .cube/token)"
export E2B_API_URL="http://127.0.0.1:$(cat .cube/port)"
export SSL_CERT_FILE="$PWD/.cube/tls/ca-bundle.pem"
```

## runbook: Linux の `*.sbx.localhost` 名前解決フォールバック

データプレーン URL の解決は OS の `*.localhost` 処理に依存する。macOS と
systemd-resolved 稼働の Linux は 127.0.0.1 へ解決される（実測）。**コンテナ・WSL2 等の
systemd-resolved 非稼働環境では解決されない**（Ubuntu 24.04 コンテナで実測）。シムは起動時に
解決可否を診断し、不解決なら `subdomain_resolution_failed` を warning で出す。

フォールバックは 2 条件を別々に満たす（TLS 検証はワイルドカード証明書 + `SSL_CERT_FILE` で
名前解決手段によらず成立する。直すのは名前解決のみ）:

1. **名前解決**: サンドボックス作成後、`/etc/hosts` に per-sandbox エントリを追記する
   （root 権限が必要。例: `127.0.0.1 49983-<sandbox_id>.sbx.localhost
   49999-<sandbox_id>.sbx.localhost`）。
2. **TLS 検証**: 追加作業不要（`ca-bundle.pem` を `SSL_CERT_FILE` に指す既定構成のまま）。

## ドライバ差し替え

バックエンドは単一の抽象（`subaco_shim.drivers.base.Driver`）に集約し、`--driver` または
`build_driver(name)` / `select_driver()` で選ぶ。`auto` は `container → podman → wslc` の順で
利用可能な最初のドライバを採り、全不在なら `RuntimeError`。契約テスト・dev では `mock` を明示指定。

```bash
subaco-shim serve --driver auto      # ホスト検出（既定）
subaco-shim serve --driver podman    # 共有カーネル（要 allow_shared_kernel オプトイン）
subaco-shim serve --driver mock      # in-memory（実コンテナ不要。CI / dev の主役）
subaco-shim status                   # 設定・.cube 状態・稼働状態を表示
```

各ドライバは自身の隔離レベルを **3 値のいずれか**（`unknown` 禁止）で宣言する。共通のドライバ抽象
契約は `tests/test_driver_contract.py` が 1 か所で表現し、mock ドライバに適用して緑にする。

## 対応 OS と実機前提

cube-shim 本体は Unix / WSL2 前提（`fcntl` を使うためネイティブ Windows 非対応）。

| OS | 既定ドライバ | 隔離レベル | 状況 |
|---|---|---|---|
| Linux | podman | shared-kernel | 実装済み（`allow_shared_kernel` オプトイン必須）。rootless 前提は下記 runbook。 |
| macOS (Apple Silicon) | container（Apple Container） | vm-per-container | スケルトン（experimental）。実装は Apple Silicon 実機で検証予定。 |
| Windows (WSL2 内) | wslc | shared-kernel | スケルトン（experimental）。GA まで暫定。DoD 未達なら podman on WSL2 を既定に。 |

実バックエンドでの e2e（コード実行・A→B 到達遮断・データプレーン到達）は **実機統合**で
検証する。契約テスト（`tests/`）は mock ドライバとコマンド列で設計意図を固定する回帰ガードであり、
実機統合とは分離している。実ドライバ契約テストは `SUBACO_SHIM_LIVE_TEMPLATE` に provision 済み
テンプレート参照を渡したときのみ走る（既定は skip）。

## runbook: podman rootless 前提（非 NixOS Linux）

非 NixOS Linux の rootless 実行はホスト側前提を要する。欠落時、シムは
`PodmanPreflightError`（`check_rootless_prerequisites()` の欠落キー付き）で明示的に失敗する。

1. `/etc/subuid`・`/etc/subgid` に現在ユーザのサブ ID 範囲を登録する
   （例: `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`）。
2. distro の `uidmap` パッケージが提供する setuid 付き `newuidmap` / `newgidmap` を用意する
   （例: Debian/Ubuntu は `sudo apt install uidmap`）。
3. 反映後に `podman system migrate` を実行する。

GitHub Actions の ubuntu ランナーはこれらが設定済みのため、CI の nightly では通常問題にならない。
NixOS では対象外（`/etc/NIXOS` 検出でスキップ）。

## 開発

```bash
just test      # pytest（stdlib 層は外部依存なしで green。SDK 契約テストは skip）
just test-wire # ワイヤ契約 E2E（実 E2B SDK 固定版。要 `just sync` + openssl）
just lint      # ruff check
just fmt       # ruff format
just compile   # py_compile のみ（オフライン確認）
```

`uv`（`uv run --with pytest pytest -q`）でオフライン実行できる。Python 3.11+ 必須
（`tomllib` / 型構文）。CI（`.github/workflows/ci.yml`）は ubuntu + macos マトリクスで
lint + test（mock ドライバ）+ wire-contract（実 SDK 契約 E2E）を回し、
podman 実コンテナ統合は nightly（`shutil.which` skip）。

## セキュリティ境界（要点）

- シムは 127.0.0.1（ループバック）にのみ bind する（`make_server` は非ループバックを `ValueError`）。
- 制御プレーンとデータプレーンの認証は相互流用不可（プレーン分離）。
- 隔離判定はホスト管理者設定のみで緩和され、エージェント申告では緩和されない。
- `.cube/token` は 0600、`.cube/` は 0700。トークンはログで伏せ字化する。
