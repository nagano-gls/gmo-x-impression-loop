# GMO サムライスタジオ X インプレッション学習ループ

公式 X（旧 Twitter）アカウントの投稿パフォーマンスを毎日蓄積し、月次で Claude に分析させて
「伸びる投稿の型」を抽出する、「ぐるぐる AI プロジェクト」思想（依頼→AI処理→ログ記録→反応収集→AI分析・学習→次回に反映）に沿った学習ループシステムです。

- **技術基盤**: GitHub Actions（Python）。GAS・Cloudflare は不使用。
- **自動投稿はしません。** 人が改善案を見て採否を判断し、次の投稿に反映する運用です。

## 全体像（3ジョブ）

| ジョブ | 頻度 | 内容 | 実装フェーズ |
|--------|------|------|------------|
| 日次: メトリクス取得 | 1回/日 | 前回取得以降の新規投稿 ＋ 既存投稿直近15件の最新値を取得し DB に蓄積 | ✅ フェーズ1 |
| 月次: Claude 分析 | 1回/月末 | その月の投稿を分析し「伸びる型」＋次月アクション案を生成 | ✅ フェーズ2 |
| 月次: Slack 通知 | 月次分析後 | 分析サマリを Slack チャンネルへ投稿 | ✅ フェーズ2 |

### 日次取得の方式（差分取得）

毎日同じ「直近N件」を取り直すのではなく、以下の2つを組み合わせて取得します。

1. **新規投稿**: DB内の最新 `tweet_id`（`since_id`）より新しいツイートのみ取得
2. **既存投稿の再取得**: 投稿日時が新しい順の既存投稿 **直近15件**（`X_REFRESH_COUNT`）を
   再取得し、インプレッション等の伸びを時系列で追跡

初回実行（DBが空）のときだけ、上記の代わりに `X_MAX_RESULTS` 件をブートストラップ取得します。

### 月次分析の方式（Claude tool use）

1. `data/impressions.db` から対象月の投稿を取得し、各ツイートの**最新の観測値**（`fetched_date` が最も新しい行）を代表値として採用
2. 投稿ごとに特徴量（文字数・画像有無・投稿時刻・曜日・リンク有無・エンゲージ率等）を付与して Claude に渡す
3. Claude には `submit_monthly_analysis` という tool を強制的に呼ばせ、構造化 JSON
   （伸びる型／伸びなかった型／次月アクション案／総括）で結果を受け取る（自由文パース失敗を防ぐため）
4. 結果を `data/reports/{YYYY-MM}.json`（構造化データ）と `.md`（人が読む用）として保存し、git にコミット
5. Slack へは `.json` の内容を整形して投稿するだけ。**Xへの自動投稿は一切行わない**

月末判定は cron で直接指定できないため、ワークフロー側で「28〜31日の毎日実行→その日がUTCで月末日かを判定→月末でなければスキップ」という方式にしている（`workflow_dispatch` の手動実行時は判定をスキップして常に実行）。

## ディレクトリ構成

```
.
├── .github/workflows/
│   ├── daily-fetch.yml       # 【日次】メトリクス取得→DBコミット
│   └── monthly-analyze.yml   # 【月次】Claude分析→Slack通知→レポートコミット
├── scripts/
│   ├── config.py           # 環境変数の読み込み・設定の一元管理
│   ├── db.py               # SQLite スキーマ / UPSERT / 月次集計クエリ
│   ├── x_client.py         # X API v2 からの取得（Tweepy）
│   ├── fetch_metrics.py    # 【日次】取得→保存のエントリポイント
│   ├── test_connection.py  # ローカル疎通確認スクリプト
│   ├── analyze_monthly.py  # 【月次】Claude 分析
│   └── post_to_slack.py    # 【月次】Slack 通知
├── data/
│   ├── impressions.db      # SQLite（初回実行時に生成・gitで追跡）
│   └── reports/             # 月次分析レポート（{YYYY-MM}.json / .md）
├── requirements.txt
├── .env.example
└── README.md
```

## データベース設計

`data/impressions.db`（SQLite）に 2 テーブル。

### `posts`（ツイート本体・1ツイート1行）
| カラム | 型 | 説明 |
|--------|----|------|
| `tweet_id` | TEXT PK | ツイート ID |
| `posted_at` | TEXT | 投稿日時（UTC ISO8601） |
| `text` | TEXT | 本文 |
| `has_image` | INTEGER | 画像添付なら 1 |
| `media_count` | INTEGER | 添付メディア数 |

### `metrics`（日次スナップショット・`tweet_id`×`fetched_date` で1行）
| カラム | 型 | 説明 |
|--------|----|------|
| `tweet_id` | TEXT | ツイート ID |
| `fetched_date` | TEXT | 観測日 `YYYY-MM-DD` |
| `impressions` | INTEGER | インプレッション |
| `likes` | INTEGER | いいね |
| `reposts` | INTEGER | リポスト |
| `replies` | INTEGER | 返信 |
| `quotes` | INTEGER | 引用 |
| `link_clicks` | INTEGER | リンククリック（※下記の注意参照） |

主キーは `(tweet_id, fetched_date)`。同日に複数回実行しても UPSERT で上書きされ重複しません。
日をまたぐと新しい行が積み上がるため、**同一ツイートのインプレの伸びを時系列で追跡**できます。

## GitHub Secrets / Variables に登録が必要なキー

リポジトリの **Settings → Secrets and variables → Actions** で登録してください。

| 名前 | 種別 | 用途 | 使用ジョブ |
|------|------|------|-----------|
| `X_API_KEY` | Secret | X API コンシューマキー（OAuth 1.0a） | 日次 |
| `X_API_SECRET` | Secret | X API コンシューマシークレット | 日次 |
| `X_ACCESS_TOKEN` | Secret | アクセストークン（自アカウントで発行） | 日次 |
| `X_ACCESS_TOKEN_SECRET` | Secret | アクセストークンシークレット | 日次 |
| `X_API_BEARER_TOKEN` | Secret | （任意）App-only Bearer。public_metrics のみのフォールバック | 日次 |
| `X_TARGET_USER_ID` | Secret | （任意）対象ユーザー ID。未指定なら `get_me()` で解決 | 日次 |
| `ANTHROPIC_API_KEY` | Secret | Claude API キー | 月次 |
| `SLACK_WEBHOOK_URL` | Secret | Slack Incoming Webhook URL（投稿先チャンネルは Webhook 発行時に指定） | 月次 |
| `ANTHROPIC_MODEL` | Variable（任意） | 使用する Claude モデル名。未設定時は既定値 `claude-sonnet-5` | 月次 |

> ワークフロー内では `X_API_KEY` 等は `secrets.X_API_KEY`、`ANTHROPIC_MODEL` のみ
> `vars.ANTHROPIC_MODEL`（機密情報ではないため Variables 側）を参照しています。

> **リンククリック数について（重要）**
> `link_clicks` は X API v2 の `non_public_metrics`（＝`url_link_clicks`）に含まれ、
> **自分のツイートかつ OAuth 1.0a ユーザーコンテキスト認証（鍵4点）でのみ取得可能**です。
> Bearer Token だけの場合は `public_metrics`（インプレ・いいね等）のみ取得でき、
> `link_clicks` は 0 固定になります。フルに取得したい場合は鍵4点を設定してください。

## ローカルでの動作確認手順

### 1. 依存関係のインストール

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 認証情報の設定

`.env.example` をコピーして `.env` を作成し、X API の鍵4点を記入します。

```bash
cp .env.example .env
# .env をエディタで開いて X_API_KEY 等を記入
```

`.env` は `.gitignore` 済みでコミットされません。

### 3. 疎通確認（DB には保存しない dry-run）

```bash
python -m scripts.test_connection
```

認証方式・取得件数・サンプル（先頭3件）が表示され、最後に `SUCCESS` が出れば疎通成功です。

### 4. 保存まで通しで確認

```bash
python -m scripts.test_connection --save
```

`data/impressions.db` が作成/更新されます。

### 5. 日次ジョブ本体を手動実行

```bash
python -m scripts.fetch_metrics
# 観測日を指定する場合:
python -m scripts.fetch_metrics --date 2026-07-23
```

### 6. 保存内容の確認（任意）

```bash
sqlite3 data/impressions.db "SELECT tweet_id, impressions, likes, link_clicks FROM metrics ORDER BY fetched_date DESC LIMIT 10;"
```

### 7. 月次分析を手動実行（`.env` に `ANTHROPIC_API_KEY` が必要）

```bash
# 対象月を省略すると実行時点のUTC月が対象になる。過去月を明示するには --month を指定:
python -m scripts.analyze_monthly --month 2026-09
```

`data/reports/2026-09.json` と `.md` が生成されます。投稿データが0件の月はスキップされ、正常終了します。

### 8. Slack通知を手動実行（`.env` に `SLACK_WEBHOOK_URL` が必要）

```bash
# 対象月を省略すると data/reports 内の最新レポートを送る
python -m scripts.post_to_slack --month 2026-09
```

### 9. GitHub Actions ワークフローの手動起動（動作確認用）

リポジトリに push 後、GitHub の **Actions** タブから `Daily Fetch X Metrics` /
`Monthly Analyze & Slack Report` を `Run workflow`（`workflow_dispatch`）で手動起動できます。
月次ワークフローは手動起動時のみ「月末日判定」をスキップして常に実行されるため、
月末を待たずに動作確認できます。

ただし対象月を省略すると**実行時点のUTC月**が対象になるため、月初に手動テストすると
その月はまだ投稿が0件で分析がスキップされ、Slack投稿も「レポートが無い」で失敗します。
`Run workflow` ボタンを押すと表示される **「対象月 YYYY-MM」入力欄** に、
データが存在する月（例: `2026-09`）を指定してテストしてください。

## 実装ステータス

- ✅ **フェーズ1**: リポジトリ初期構築、X API 取得（`x_client.py`）、SQLite 保存（`db.py`）、
  日次エントリ（`fetch_metrics.py`）、疎通確認（`test_connection.py`）
- ✅ **フェーズ2**: GitHub Actions ワークフロー（`daily-fetch.yml` / `monthly-analyze.yml`）、
  月次 Claude 分析（`analyze_monthly.py`）、Slack 通知（`post_to_slack.py`）
