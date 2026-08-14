# E2B ワイヤ spike 成果物（M2a-1）

`docs/00-memo/05_spike結果_E2B_ワイヤ.md` の実測に使った偽サーバー・キャプチャ・決定実験の
一式（e2b==2.30.0 / e2b-code-interpreter==2.8.1 固定で実測。**判定: full-fidelity-feasible**）。
M2a の実装時に契約テスト（実 SDK をクライアントに使う E2E——レポート §6-7）の雛形として使う。

| ファイル | 役割 |
|---|---|
| `capture_server.py` | 全リクエストのメソッド・パス・ヘッダ・ボディを JSON lines で記録する反復構築型の偽 E2B サーバー |
| `capture.jsonl` | 偽サーバーへの全系統（create → files → run_code → kill）実測キャプチャログ |
| `wire_capture.py` | e2b_connect の Connect RPC ワイヤ形式（JSON codec・エンベロープ）の実測 |
| `mock_execute_server.py` | run_code（POST /execute、chunked JSON lines）を満たす最小 mock |
| `spike_control_plane.py` | 制御プレーン 3 エンドポイントの必須キー・エラー形の実測 |
| `spike_url_matrix.py` | E2B_DEBUG / E2B_SANDBOX_URL / domain 各方式の URL 構成マトリクス実測 |
| `spike_rundcode_tls.py` | **決定実験**: ポート埋め込み domain + `*.sbx.localhost` ワイルドカード TLS + SSL_CERT_FILE で SDK 無改造の create → run_code → kill が green（`RESULT: GREEN`） |
| `reverify_tls_envd.py` | 敵対的検証者による追試（envd 面の TLS 経由実測） |

実行は `uv sync --extra test` 済みの環境で `uv run python spikes/e2b-wire/<script>`。
TLS 証明書はスクリプトが一時生成する（鍵はコミットしない）。
