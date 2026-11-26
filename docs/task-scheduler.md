# Automation 定期起動

## 構成

GitHub Actions の標準 `schedule` を使い、`.github/workflows/task-scheduler.yml` から `scripts/task_scheduler.py` を実行する。スクリプトは同じリポジトリの承認済み workflow に `workflow_dispatch` API を送る。ChatGPT の予約機能で30分間隔を設定する構成ではない。

子 workflow は既存の処理と GitHub Secrets をそのまま使う。送信先ブランチは `main`、子 workflow への任意の `inputs` は指定しない。

## 承認済みの起動時刻

以下は **APIを送信する目標時刻（日本時間 / Asia/Tokyo）**。cronによる準備開始時刻とは異なる。

| 処理 | group | 起動先 | API送信の目標時刻 |
|---|---|---|---|
| 金レイド・自発 | `raid` | `G1_test.yml`〜`G6_test.yml`、`J1_test.yml`、`J2_test.yml` | 毎時10分・40分 |
| あいさつ送信 | `greeting` | `greeting.yml` | 毎日03:20 |
| 統合日課 | `daily` | `daily_test.yml` | 毎日09:17 |
| ノーマル召喚 | `normal` | `NormalSummon.yml` | 毎日17:20 |
| ノーマルモンスター消化 | `enhance` | `Enhance.yml` | 毎日23:20 |
| サブ聖戦 | `sub` | `sub.test.yml` | 毎日09:59:30・11:59:30 |
| 夜の聖戦 | `holy` | `test.yml` | 毎日20:59:30・21:59:30 |

予定どおり全枠を送信した場合、子 workflow は14種類・392起動/日となる。これは設定上の件数であり、成功実績ではない。

統合日課には、ログボ、聖戦報酬、レイド報酬、ミッション、挨拶既読、**レア本購入・AP薬購入**、最終プレゼント回収が含まれる。購入だけをこのschedulerの入力で切り替える機能はない。

旧設定の `J3`〜`J7`、`G7`〜`G10`、`Collection.yml`、`mission.yml`、`deletegreeding.yml`、`Summon.yml`、`enhance_trainer.yml`、`SUB3_HOLYWAR.yml`、その他の臨時用 workflow は対象に含めない。`mt.yml` への置換も行わない。

## 準備開始とAPI送信

cronは目標時刻より前にschedulerを起動する。schedulerは該当する予定枠を確認し、目標時刻まで待ってからAPIを送信する。cronの時刻をそのままゲーム処理の開始時刻として解釈しない。

GitHubのcronは分単位の指定で、秒指定を直接表現できない。聖戦の `:59:30` はscheduler内の待機で扱う。API受付後には子 workflow のキュー待ち・環境準備もあるため、API送信時刻とゲーム内処理開始時刻も一致するとは限らない。

遅れて起動した予定枠を無制限に追いかけない。実装の受付期限を過ぎた枠はスキップとして記録し、次の予定枠へ勝手に振り替えない。

## 認証・重複防止・結果の扱い

- 実行時の認証は `GITHUB_TOKEN` を使用する。必要な権限は `contents: read` と `actions: write`。長期PATやn8n添付の認証値をリポジトリへ保存しない。
- 対象は表の固定一覧に限定する。送信前に、同じ workflow・ブランチの実行中runと、同じ予定枠の起動履歴を確認する。
- 重複や実行中が見つかった場合は重ねて起動しない。照会に失敗した場合も「実行なし」とみなして送信しない。
- POSTの応答が失われた場合、受付済みの可能性があるため無条件に再送しない。run IDや直近の実行履歴を照合し、曖昧な状態をそのまま成功扱いしない。
- schedulerの受付結果と、子 workflow の完了結果は別に扱う。APIが成功しても、子の失敗・キャンセルやゲーム内の部分未完了が解消したことにはならない。
- 子の完了までschedulerの直列実行枠を占有しない。受付後のrun ID・URLを使って子の結果を確認する。

## 手動での予定確認

親 workflow の `workflow_dispatch` 入力は次のとおり。これらはscheduler自身の操作入力であり、子 workflow へ転送しない。

| 入力 | 値・用途 |
|---|---|
| `mode` | `plan`（既定）または `dispatch`。`plan` は起動APIを送らず予定と対象を確認する |
| `group` | `auto`、`raid`、`greeting`、`daily`、`normal`、`enhance`、`sub`、`holy` |
| `slot` | 任意のISO 8601日時。指定する場合は `+09:00` や `Z` などのタイムゾーンを明記する |

`dispatch` は実際の起動操作になる。過去の枠の再送や、一覧外の処理を起動する目的には使わない。

## 運用上の限界と停止

GitHubのscheduleには混雑時の遅延があり、キューに入った処理がドロップされる場合もある。早めの準備開始は遅延を吸収するための措置で、定刻起動の保証ではない。特に聖戦の30秒前起動は、実際の受付時刻と子の開始時刻を確認する。

scheduleはdefault branch上のworkflowで動く。publicリポジトリでは60日間repository activityがないと定期実行が自動無効化され得るため、長期運用ではworkflowの有効状態も確認する。

以後の定期起動を止める場合は、対象リポジトリを明示して次を実行する。

```sh
gh workflow disable task-scheduler.yml --repo sickness8484/browser-automation
```

これはschedulerの停止操作で、すでに受付済み・実行中の子runを取り消す操作ではない。停止時はActionsの実行一覧も確認する。

## 公式仕様

- [workflowから別workflowを起動する場合のGITHUB_TOKENの扱い](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [scheduleの仕様・遅延・自動無効化](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [workflow_dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
