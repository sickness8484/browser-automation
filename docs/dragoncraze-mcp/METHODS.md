# MCPメソッド契約案 v0.1

状態: 設計。以下の関数が実装済み・配備済みであるという意味ではない。
ゲーム側の実route、selector、認証仕様は現行HTMLを使って別途検証する。未検証値を補って実行しない。

## 共通規約

- account_id: 管理者が登録したopaque ID。OAuthの主体に紐づく所有/許可リストと照合する。
- snapshot_id: アカウント・取得時刻・parser versionに結び付けたサーバー側の状態参照。
- action_id: 検証済みアダプターが最新画面から生成した操作候補のopaque ID。
- request_id: トレース用ID。秘密を埋め込まない。
- idempotency_key: 同じ認証主体・アカウント・操作を誤送信しないための識別子。同じkeyで内容が異なる場合は拒否する。
- 全入力で不明なプロパティを拒否。大きさ、整数範囲、期限を検証。
- get_state等のread-only分類はゲーム上の副作用がないことを確認してから付与する。
- annotationsはMCPクライアントへの説明。サーバーの認可/実行制限の代わりにはならない。
- 全変更ツールはreadOnlyHint=false。不可逆操作はdestructiveHint=true。ゲームへのアクセスはopenWorldHint=true。
- エラー: AUTH_REQUIRED、ACCOUNT_FORBIDDEN、SESSION_EXPIRED、UNSUPPORTED_PAGE、STALE_SNAPSHOT、INVALID_INPUT、BUDGET_EXCEEDED、RATE_LIMITED、ACTION_UNKNOWN、NOT_IMPLEMENTED。

## dc_health()

入力: なし。
出力: service、version、protocol、live_enabled、capabilities（implemented/disabled/unverifiedを区別）。
副作用: なし。ゲーム通信も資格情報の検査も行わない。
目的: MCP接続そのものの確認。健康な応答をゲーム操作成功と解釈しない。

## dc_list_accounts()

入力: なし。
出力: accounts[{account_id, label, session_status, allowed_methods}]。
認可: 認証主体が利用できるアカウントだけ返す。
秘密: 認証URL、Cookie、login tokenは返さない。

## dc_get_state({account_id, section})

section: 実装済みのprofile/rewards等の列挙値のみ。汎用URLを受け付けない。
出力: snapshot_id、observed_at、expires_at、page_type、parsed_state、parse_warnings、source_version。
動作: 既知の読み取り経路で状態を取得し、必要な値だけ構造化する。
停止: login画面、未知のHTML、parser不一致、サイズ超過で停止する。
読み取れなかった数値はnull等で未測定と示し、0とみなさない。

## dc_list_actions({account_id, snapshot_id})

出力: actions[{action_id, kind, title, target_summary, estimated_cost, reversible, expires_at}]。
動作: 保存したsnapshotから検証済みの操作候補だけ生成。hidden fieldsはWorker内部に保持。
副作用: ゲームへ変更要求を送らない。
未知の費用・操作種別・曖昧な複数formは実行可能候補に昇格させない。

## dc_prepare_action({account_id, snapshot_id, action_id, limits})

limits: 許可する資源消費量・対象件数・所要操作回数。省略時に無制限へ拡張しない。
出力: plan_id、plan_digest、expires_at、対象と操作の要約、消費上限、必要な確認。
動作: 主体・アカウント・操作・対象・消費上限を固定した短命の実行計画を作る。
副作用: 準備データの保存だけ。ゲーム変更はまだ送らない。
認可: 計画が存在するだけではユーザー同意の証拠にならない。クライアント側の承認とサーバー側の権限を別途検証する。

## dc_execute_action({plan_id, plan_digest, idempotency_key})

出力: request_id、job_id、status（succeeded/failed/unknown/queued）、observed_effects、stop_reason。
条件: 認証主体、対象アカウント、plan期限/digest、必要権限、消費制限、最新状態の適合を確認。
動作: 同一アカウントlock → 事前状態の再検証 → 実行意図の永続化 → 確定した操作のみ送信 → 成功条件の照合 → 結果保存。
重要: stateが変化したら勝手に別対象を選ばずSTALE_SNAPSHOTを返す。部分成功と不明を区別する。
重要: ネットワーク不明時に「多分失敗」として再送しない。unknownを返して状態確認へ進む。

## dc_get_job({job_id})

出力: owner-visibleなstatus、開始/終了時刻、実行件数、確認済みの効果、失敗/保留理由。
副作用: なし。認可外主体にはjob内容を返さない。
機密: raw HTML、認証URL、Cookie、hidden fieldsを返さない。

## dc_cancel_job({job_id})

副作用: 未実行ステップを停止する。完了済みまたは送信中のゲーム変更の取り消しではない。
出力: cancel_requested、stopped_before_next_step、already_completed等を区別。

## 後で追加する分野別メソッド

- 報酬: 候補を読む → 対象と上限を準備 → 指定分を受け取る。
- 合成: おまかせ合成候補を読む → 消費素材と結果を提示 → 承認範囲だけ実行。別種の合成へ自動切替しない。
- レイド: 状態・対象を読む → 許可した技/資源上限を準備 → 1つの確定操作を実行。

これらは用途例であり、現在のユーザーが全操作・全対象・全資源消費を承認したとは解釈しない。
定期ジョブの作成・頻度・継続時間は、直接ツール呼出しの検証後に別の契約として定義する。
