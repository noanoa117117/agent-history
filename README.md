# agent-history

Ubuntu 上で AI と人間の作業履歴を SQLite に保存し、SQLite FTS5 で検索するローカル CLI です。Codex、Claude Code、ブラウザ版 AI、シェル操作などの観測可能な入出力を、将来の作業再利用や記事生成に渡せるコンテキストとして蓄積することを目的にしています。

この MVP は自動収集を行いません。利用者が CLI で session と event を登録します。

## なぜ project 中心ではなく session 中心か

1 回の作業は、複数の repository、directory、host、service、cloud resource、topic にまたがることがあります。Cloudflare や Azure のようにローカルディレクトリを持たない対象もあります。そのため、一次単位をローカルの `project` や `root_path` に固定せず、作業の時間的なまとまりである `session` としています。

`targets` は作業対象を補助的に表します。1 つの session に複数 target を関連付けられ、分類に失敗しても `events` の一次データは失われません。

## データモデル

- `sessions`: source、model、開始・終了時刻、初期作業ディレクトリ、タイトルなど。session が一次単位です。
- `events`: 観測可能な prompt、assistant message、command、tool result、file change、error、メモなど。session 内で `sequence_no` を持ちます。
- `targets`: repository、service、cloud resource、topic などの作業対象。`target_type` と `slug` が一意です。
- `session_targets`: session と target の多対多関連。relation、confidence、assigned_by を保存します。
- `events_fts`: `events.content` を検索する FTS5 インデックス。イベントの INSERT、UPDATE、DELETE を SQLite トリガーで同期します。

AI 内部の非表示な思考過程は保存対象にしません。保存対象は観測可能な入出力、コマンド、ツール結果、メモ、エラーなどです。SQLite 内の `events` が一次情報で、`facts.md` や `draft.md` は作成しません。

## セットアップ

追加パッケージは不要です。Ubuntu の Python 3.9 以上を想定しています。SQLite が FTS5 を有効にしている必要があります。

```bash
cd /home/amida/projects/agent-history
./bin/agent-history --help
```

開発環境では `src` を Python パスに追加すれば、モジュールとしても実行できます。

```bash
PYTHONPATH=src python3 -m agent_history --help
```

DB パスは次の優先順位で決まります。

1. `--db PATH`
2. 環境変数 `AGENT_HISTORY_DB`
3. `<repository-root>/data/agent_history.db`

## DB 初期化

```bash
./bin/agent-history init
```

DB ディレクトリを作成し、スキーマを冪等に適用します。DB パスと FTS5 の利用可否を表示します。FTS5 が使えない SQLite では明確なエラーで終了します。接続時には `PRAGMA foreign_keys = ON` と `PRAGMA journal_mode = WAL` を有効化します。再実行すると FTS インデックスも `events` から再構築するため、インデックスだけが壊れた場合の修復手段になります。

初期化前に他のコマンドを実行すると、`error: database is not initialized (...); run \`agent-history init\` first` を表示して終了コード 1 で終わります。

リポジトリの `data/agent_history.db` は初期化済みの空 DB です。動作確認で登録したデータは残していません。DB 本体は Git 管理対象外です。

## 基本 CLI

### Session

通常の `session-start` は、シェル変数へ代入しやすいよう session ID だけを標準出力へ出します。

```bash
SESSION_ID="$(${PWD}/bin/agent-history session-start \
  --source codex \
  --model gpt-5.6-luna \
  --cwd /home/amida/projects/agent-history \
  --title "agent-history MVP implementation")"

./bin/agent-history session-list
./bin/agent-history session-list --source codex --limit 20
./bin/agent-history session-show "$SESSION_ID"
./bin/agent-history session-end "$SESSION_ID"
```

`session-end` は `ended_at` を UTC ISO 8601 で設定し、`capture_status` を `completed` にします。存在しない ID はエラーです。終了済み ID に再実行した場合は、既存値を変更せず `Session already ended` と表示する冪等動作です。

### Event

```bash
./bin/agent-history event-add \
  --session "$SESSION_ID" \
  --type user_prompt \
  --actor human \
  --content "CockpitをCloudflare Tunnelで公開したい"

./bin/agent-history event-add \
  --session "$SESSION_ID" \
  --type command_result \
  --actor tool \
  --content-file ./command-output.txt \
  --cwd /home/amida/projects/agent-history \
  --exit-code 0
```

`--content` と `--content-file` は排他です。`sequence_no` は session ごとに `BEGIN IMMEDIATE` 内で採番します。`occurred_at` を省略すると現在 UTC 時刻になります。保存時刻はタイムゾーン付き UTC ISO 8601 です。

JSON は次のように入力できます。JSON として検証し、Python の値を一度だけシリアライズして保存します。

```bash
./bin/agent-history event-add \
  --session "$SESSION_ID" \
  --type tool_result \
  --actor tool \
  --content-json '{"ok":true,"exit_code":0}'
```

### Target と関連付け

```bash
CLOUDFLARE_TARGET="$(${PWD}/bin/agent-history target-add \
  --type service --slug cloudflare --name Cloudflare --locator cloudflare)"

COCKPIT_TARGET="$(${PWD}/bin/agent-history target-add \
  --type service --slug cockpit --name Cockpit --locator localhost:9090)"

./bin/agent-history session-target-add \
  --session "$SESSION_ID" --target "$CLOUDFLARE_TARGET" \
  --relation configured --confidence 1.0 --assigned-by manual

./bin/agent-history session-target-add \
  --session "$SESSION_ID" --target "$COCKPIT_TARGET" \
  --relation configured --confidence 1.0 --assigned-by manual

./bin/agent-history target-list
./bin/agent-history target-list --type service
```

同じ `target_type` と `slug` の target を再登録しても重複行は作らず、既存 ID を返します。既存値を変更したい場合だけ `--update` を付けます。同じ session-target の関連付けを再実行した場合は relation、confidence、assigned_by を更新する冪等動作です。

## 全文検索

```bash
./bin/agent-history search "cockpit cloudflare"
./bin/agent-history search "Cockpit" \
  --target cloudflare \
  --source codex \
  --context-before 1 \
  --context-after 1
./bin/agent-history search "Cockpit" --from 2026-01-01 --to 2026-01-31
./bin/agent-history search "Cockpit" --json
```

検索語は SQL パラメータとして渡し、FTS5 の構文へそのまま展開しないよう各語を引用します。複数語は AND 検索です。`--target` は target の slug で session を絞り込みます。`--from` と `--to` は ISO 日付または日時を受け付けます。日付だけの場合は UTC の日全体を対象にします。

### 検索の仕組みと制約

検索は **FTS5 の MATCH と、`events.content` に対する LIKE 部分一致の和集合** です。FTS5 標準の unicode61 tokenizer は日本語を分かち書きしないため、`CockpitをCloudflare Tunnelで公開したい` のような日英混在文は 1 トークン扱いになり、MATCH だけでは `Cockpit` を見つけられません。LIKE のフォールバックがこれを補います。

この設計には次の副作用があります。

- **部分一致します。** 語の途中でもヒットします。例えば `cat` は `concatenate` にヒットします。FTS5 単独の語単位検索とは挙動が異なります。
- **LIKE 側は全表スキャンです。** event 数が増えるほど検索が遅くなります。日本語検索をインデックスで行いたい場合は、FTS5 の `trigram` tokenizer 導入が次の選択肢です（Stage 2 の検討事項）。

`export` は内部的に最大 1000 件の event を対象にします。

デフォルト表示は人間向けです。`--json` では `event_id`、`session_id`、`sequence_no`、`occurred_at`、`source`、`event_type`、`actor`、`content`、`targets` などを含む JSON 配列を出します。context を指定すると同じ session の前後 event も追加され、複数のヒットで重複する event は除去されます。context event には `matched: false` が付きます。

## Markdown export

```bash
./bin/agent-history export \
  --query "cockpit cloudflare access" \
  --target cloudflare \
  --output /tmp/cockpit-context.md

cat /tmp/cockpit-context.md
```

検索結果を session ごとの Markdown にまとめます。export は記事本文を生成せず、将来 `tech-writing` へ渡すコンテキストだけを作ります。`--output` を省略すると標準出力へ出します。

## サニタイズ

既定では保存前に以下を検出します。

- PEM private key ブロック
- `api_key`、`token`、`secret`、`password` などのラベル付き値
- AWS Access Key 形式
- Bearer token
- URL クエリの `token`、`key`、`secret` など
- メールアドレス
- 有効な IPv4 アドレス

ラベルは接頭辞付きでも検出します。`token: x` だけでなく `GITHUB_TOKEN=x`、`DB_PASSWORD=x`、`X-Auth-Token: x`、`my_secret = x` も置換対象です。`content_json` の場合は、キー名が secret 系の語で終わる値（`GITHUB_TOKEN` など）も置換します。

置換値は `<REDACTED_SECRET>`、`<REDACTED_EMAIL>`、`<REDACTED_IP>`、`<REDACTED_PRIVATE_KEY>` です。`example.com` 系ドメイン、`user@example.com`、`<ACCOUNT_ID>`、`<INTERNAL_IP>`、`<CLIENT_SECRET>` などの既知の例・プレースホルダーは保持します。

IPv4 判定では、`Ubuntu 24.04.1.2` のようにゼロ詰めの桁を含むドット区切り数値をバージョン文字列とみなして保持します。逆に `3.14.1.2` のようにゼロ詰めがないバージョン文字列は IP として置換されます。秘密情報の保護を優先し、過剰に伏せる側へ倒しています。`password reset`、`PasswordAuthentication no`、`tokenizer` のような散文は置換しません。

置換があれば `sensitivity=sanitized`、なければ `clean` です。原文は既定では保存しません。`--no-sanitize` は明示指定時だけ使え、原文を `sensitivity=raw` として保存します。将来的な raw 保存領域はまだ実装していません。

## バックアップ

DB 本体は WAL モードで動作するため、実行中の DB をコピーするより SQLite のバックアップ API や `.backup` を使う方が安全です。CLI の DB が停止中であることを確認したうえで、簡単なバックアップは次のようにできます。

```bash
sqlite3 data/agent_history.db ".backup '/tmp/agent_history-backup.db'"
```

DB 本体、`-shm`、`-wal` は `.gitignore` で Git 管理対象外です。秘密情報を含む可能性があるため、バックアップ先の権限にも注意してください。

## 現在の制約

- Codex CLI、Claude Code、ブラウザ会話、シェル履歴を自動収集しません。event は CLI から明示登録します。
- 日本語の形態素解析は導入していません。FTS5 の標準 tokenizer に LIKE 部分一致を併用するため、語単位ではなく部分一致になり、event 数が増えると LIKE 側の全表スキャンが遅くなります。
- 認証・認可、HTTP 受信 API、Web UI はありません。単一ユーザーのローカル利用を前提にしています。
- サニタイズは高信頼の正規表現ベースで、すべての秘密情報を保証するものではありません。保存前に内容を確認してください。
- ベクトル検索、AI 自動タグ付け、記事生成、provenance 管理は未実装です。

## 今後のロードマップ

### Stage 2

- Codex ログ収集
- Claude Code Hooks 連携
- Git 差分収集

### Stage 3

- ブラウザ拡張
- ブラウザ会話を Ubuntu へ送信
- 重複検出
- 認証付きローカル API

### Stage 4

- `tech-writing` 連携
- AI による関連イベント選択
- Qiita/Zenn 記事生成
- provenance 保存

Codex CLI の自動ログ収集、`codex-logged` / `claude-logged` ラッパー、ChatGPT ブラウザ会話の自動取得、スクリーンショット保存、Qiita/Zenn 投稿なども将来構想であり、現在は実装していません。

## 開発とテスト

```bash
python3 -m compileall src
bash -n bin/agent-history
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

テストは一時ディレクトリの SQLite DB を使用し、本番 DB へ書き込みません。
