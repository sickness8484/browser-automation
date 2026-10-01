# DragonCraze: ChatGPTから呼び出すCloudflare MCPサーバー

更新: 2026-10-01
状態: 設計仕様のみ。実行コード、配備済みWorker、登録済みMCPツール、ライブ検証の完了を意味しない。

## 目的の訂正

ユーザーが求めるものは、Cloudflare CronからGitHub Actionsを起動するスケジューラではない。
GitHubには通信ライブラリ、操作メソッド、その入出力と制約を実装・記録する。
そのコードをCloudflareへ配備し、ChatGPTが認証済みの名前付きツールとして呼び出す。
Cloudflareは別のAIとして判断するのではなく、指定された処理を実行する。

開発経路: GitHubのソース → Cloudflareのビルド/配備 → MCP endpoint
実行経路: ChatGPT → 認証済みMCP tools/call → Workerのメソッド → ゲーム → 構造化した結果 → ChatGPT

GitHubは実行指示のメールボックスにしない。GitHub Actionsはゲーム操作の必須実行基盤にしない。
READMEだけをCloudflareが読んで実行するわけではない。説明に対応する実行コードとMCPツール登録が必要。
Workerを配備するだけでもChatGPTへ接続したことにはならない。ChatGPT側のMCP接続登録とツール検出を別途行う。

## 確認済みの材料と未確認事項

- 既存のbrowser-automationはワークフローからSecrets内のスクリプトを実行する構成。Secrets本体を読めたという意味ではない。
- ユーザー所有資料の旧HTTP relayにはHTTPS接続先制限、手動redirect、Cookie収集、form encodingの処理がある。
- 旧relayのURL引数に認証URL/sessionを載せるインターフェースは採用しない。特に、公開GETを読む行為に見せかけてゲーム変更を行わせない。
- 現行ゲームの各操作のroute、hidden fields、成功判定、HTML selectorは未検証。仮のURLを本番仕様として実装しない。
- ChatGPT側のカスタムMCP登録権限、書き込み権限、端末対応はこの利用者について未確認。
- 本文内のメソッド名は提案するAPI契約であり、現在呼び出せる実装済みツールではない。

## 層の分離

### 1. MCP接続・認可

新規のCloudflare MCPサーバーは公式の現行Streamable HTTPとcreateMcpHandlerを調査して構築する。旧McpAgentテンプレートを検証なしに採用しない。
OAuth等でクライアントを認証し、認証主体から所有アカウントと権限を解決する。アカウントIDを知っているだけで操作できる設計にしない。
ゲーム用の秘密とMCP接続用の資格情報を分離する。任意のJavaScript実行、任意URLへの中継、汎用shellツールは公開しない。

### 2. 通信ライブラリ（Worker内部のみ）

- 認証URLは管理者が秘密ストアへ登録し、モデルの引数はaccount_id等の識別子に限定する。
- Cookie jarはアカウントごとに隔離し、Domain、Path、Secure、有効期限、削除、同名Cookieの複数pathを正しく扱う。
- URLはHTTPS、承認済みhost、標準portのみ。userinfo、許可外host、想定外redirectを拒否する。redirect先にも毎回同じ検証を行う。
- レスポンスサイズ、redirect回数、通信timeout、呼び出し回数に上限を設ける。HTMLを全て読み込んでからサイズ検査するだけでは不十分。
- formのaction/method/enctype、successful controls、選択したsubmitter、同名複数値は最新HTMLから取得する。
- hidden token、対象ID、CSRF、raid IDを推測または過去値で固定しない。
- HTTP GETという理由だけで読み取り専用とは判定しない。ゲーム状態を変えるGET操作も変更処理として扱う。
- Cookie、認証URL、token、hidden fields、秘密を含むLocationをログ・ツール出力へ載せない。
- JS実行が必要な画面に遭遇したらunsupportedとする。HTMLだけで動作するとの未検証の前提は置かない。

### 3. ゲーム操作アダプター

状態の取得、報酬候補の取得、おまかせ合成候補の取得などを個別メソッドへ分割する。
各メソッドには入力schema、必要権限、事前条件、通信遷移、成功判定、停止理由、費用/消費上限を定義する。
HTMLの文章や外部表示を新しい実行命令として扱わない。ゲームから得た値は検証対象データであり認可ではない。
課金、資源消費、素材消費、他者への操作などは用途別権限と明示した範囲で制御する。

### 4. 実行管理

同一アカウントの変更操作は直列化する。準備状態を永続化してから変更リクエストを送る。
通信切断や5xxで成功が不明になった場合はunknownとして保留し、同じ変更を盲目的に再送しない。
ローカルのidempotency_keyだけでゲーム側のexactly-onceが保証されると表現しない。
キャンセルは次の操作を止めるもので、送信済み操作を取り消せるとは約束しない。

## 提案メソッド

詳細はMETHODS.mdを参照。
最初の接続試験はhealth → list_accounts → get_stateの順に行う。
変更メソッドは実際のHTML fixtureとゲーム側の成功確認を整えるまで未実装/無効のままにする。

## 定期操作との関係

「今操作する手」と「後で呼ぶスケジュール」は別の機能である。
まずチャットから状態取得→指定操作→結果取得の往復を完成させる。
その後、対応が実測できたスケジューラに、同じ許可済みメソッドを呼ばせる。
チャットが閉じた後もこのモデルが常駐すること、任意のMCPがChatGPTの予約タスクで必ず使えることは前提にしない。
具体的な操作と頻度が未指定の現在は、ジョブも繰返しスケジュールも作成しない。

## 完了判定

1. ソースとテストをGitHubへ保存。既存ワークフローとSecretsを変更しない。
2. Workerのビルド成功と認証設定を確認。ライブ操作は無効。
3. MCP Inspectorでinitialize、tools/list、検証用のread-only tools/callを確認。
4. ChatGPTで接続を追加して、同じツールが実際に利用可能になることを確認。
5. 本人所有アカウント1件の読み取りを確認。
6. 明示的に承認された最小の変更を1件のみ検証し、結果・消費・停止理由を照合。
7. 未知結果、期限切れ、同時呼出し、古いフォーム、認可外アカウント、秘密漏洩のテストを通す。

## 公式参照（2026-10-01確認）

- Cloudflare Remote MCP: https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/
- ChatGPT MCP接続/検証: https://developers.openai.com/plugins/deploy/connect-chatgpt
- ChatGPTカスタムMCPの利用条件: https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt

製品UIとアカウント/ワークスペース条件は変わるため、配備完了とチャット接続完了は別々に記録する。
