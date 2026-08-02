# agent-history

Ubuntu 上で AI と人間の作業履歴を SQLite に保存し、SQLite FTS5 で検索するローカル CLI です。Codex、Claude Code、ブラウザ版 AI、シェル操作などの観測可能な入出力を、将来の作業再利用や記事生成に渡せるコンテキストとして蓄積することを目的にしています。

Stage 1 では利用者が CLI で session と event を登録します。Stage 2 では Claude Code の command hooks を単一のローカル受信アダプターへ集約し、観測可能なライフサイクルイベントを自動保存できます。

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

リポジトリの `data/agent_history.db` は初期化済みです。現在の作業ツリーにはfixtureによる Claude Hook 手動確認データが入っています（`source_session_id=claude-fixture-session`）。不要なら、DBを停止した状態でバックアップ後に利用者の判断で整理してください。DB 本体は Git 管理対象外です。

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
- **LIKE 側は全表スキャンです。** event 数が増えるほど検索が遅くなります。日本語検索をインデックスで行いたい場合は、FTS5 の `trigram` tokenizer 導入が次の検索改善候補です。

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

## Stage 2: Claude Code Hooks

Claude Code の stdin JSON を `bin/agent-history-claude-hook` で受け取り、既存の SQLite DB へ保存します。Hook は外部通信をせず、AI 内部の非表示な思考過程や transcript 本文を読み込みません。保存するのは Claude Code が Hook に渡す観測可能な入力・出力、ツール名、入力・結果の制限付き投影、通知、エラー、作業ディレクトリなどです。

### 対応イベント

ローカルで確認した Claude Code は `2.1.220` です。同バージョンのバイナリが持つ Hook イベント定義（31 種）と照合済みで、アダプターはその全てを処理します。

`SessionStart`, `SessionEnd`, `Setup`, `InstructionsLoaded`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionDenied`, `Notification`, `MessageDisplay`, `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `Stop`, `StopFailure`, `TeammateIdle`, `ConfigChange`, `CwdChanged`, `FileChanged`, `DirectoryAdded`, `WorktreeCreate`, `WorktreeRemove`, `PreCompact`, `PostCompact`, `Elicitation`, `ElicitationResult`

`matcher` を受け付けるイベントも同バイナリの定義に合わせています（`"*"` は match-all として扱われます）。イベント名と matcher 対象は `tests/test_claude_hooks.py` に固定してあり、将来の Claude Code で名前が変わった場合はテストが失敗します。Claude Code は未知のイベント名を警告付きで無視するため、ずれても設定は壊れませんが、そのイベントは収集されなくなります。

イベントは一般化した `event_type` と `actor` に変換し、元の Hook 名は `content_json.hook_event` に残します。`PreToolUse` は `tool_call/claude`、`PostToolUse` は `tool_result/tool`、`PostToolUseFailure` は `tool_error/tool`、`UserPromptSubmit` は `user_prompt/human`、`Stop` は `assistant_stop/claude` などへ変換します。未知のツールも汎用的に保存します。

`WorktreeCreate` だけは受動的な記録 Hook として設定していません。Claude Code の公式仕様では、この Hook が成功すると作成した worktree のパスを stdout に返す必要があり、記録専用アダプターが登録されると標準の worktree 作成を置き換えてしまうためです。アダプター自体は入力を受けた場合の変換を実装しています。`FileChanged` は matcher を取らないイベントなので、matcher なしで登録します。

### インストールとアンインストール

デフォルトはドライランです。ユーザー設定 `~/.claude/settings.json`（`CLAUDE_CONFIG_DIR` があればその配下）を読み取り、変更予定だけを表示します。

```bash
./scripts/install-claude-hooks
./scripts/install-claude-hooks --apply
./scripts/uninstall-claude-hooks
./scripts/uninstall-claude-hooks --apply
```

`--apply` を付けたときだけ設定を変更します。既存の `hooks`、`permissions`、その他の設定は保持し、同じアダプターを二重登録しません。変更前に `settings.json.bak-<UTC timestamp>` を作成し、JSONを検証してから同じディレクトリ内の一時ファイルを原子的に置換します。`--scope project` または `--scope local` と `--settings-path` も利用できます。不正JSONやシンボリックリンクの設定ファイルは変更しません。

設定例は [config/claude-hooks.example.json](config/claude-hooks.example.json) にあります。`$AGENT_HISTORY_ROOT` は例示用の変数なので、手動利用時はリポジトリの絶対パスを環境変数へ設定するか、インストールスクリプトを使ってください。

### 保存、サイズ制限、障害時の動作

Claude の `session_id` を `sessions.source_session_id` に保存し、`SessionStart` がなくても最初のイベントからDB sessionを自動作成します。同じ Claude session に対する `SessionStart` の再送は重複登録しません。`SessionEnd` が届けば `capture_status=completed` にしますが、異常終了では届かない場合があります。

イベントの `content` は検索しやすい短い本文、`content_json` はサニタイズ済みの構造化ペイロードです。JSONには利用可能な値だけを入れ、`original_size`、`stored_size`、`truncated` を記録します。既定値は本文 64 KiB、構造化JSON 256 KiB です。

```bash
export AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES=65536
export AGENT_HISTORY_HOOK_MAX_JSON_BYTES=262144
```

処理順序は「入力上限 → JSON解析 → サニタイズ → 安全な切り詰め → 保存」です。切り詰めはサニタイズの後に行うため、秘密情報の後半だけが残ることはありません。大きな文字列は先頭と末尾を残して UTF-8 境界を壊さずに切り詰め、空白を含まない base64 形式の巨大な値だけを `<REDACTED_BINARY>` に置き換えます（通常の長文は切り詰めるだけで捨てません）。

stdin 全体が上限（既定 4 MiB、`AGENT_HISTORY_HOOK_MAX_JSON_BYTES` の 16 倍）を超えた場合は、切り詰めたJSONを安全に解析できないため、そのイベントは保存せず dead-letter に理由とハッシュだけを記録します。DB書き込みに失敗した場合は、サニタイズ済みJSONを `data/dead-letter/`（または設定した `AGENT_HISTORY_HOOK_DEAD_LETTER_DIR`）へ 0600 で保存し、診断情報を `data/logs/claude-hooks.log`（または `AGENT_HISTORY_HOOK_LOG`）へ最小限記録します。dead-letter も失敗した場合はエラー種別だけを試みて記録します。

記録 Hook は stdout に何も出さず、成功・失敗のどちらでも原則終了コード 0 を返します。Claude Code の動作をブロックする終了コードや外部API呼び出しは行いません。ログとdead-letterはローテーションしないため、長期運用ではサイズを監視してください。

### サニタイズとトランスクリプト

既存の `sanitizer.py` を再帰的に通し、ネストした辞書・配列、シェル形式、HTTPヘッダー、URLクエリ、秘密情報らしいJSONキーを処理します。API key、token、secret、password、Bearer、Cookie、AWS形式キー、メールアドレス、IPv4、PEM秘密鍵などは既定で置換されます。`<CLIENT_SECRET>`、`<ACCOUNT_ID>`、`<API_TOKEN>`、`example.com` 系の例は保持します。原文は保存しません。

`transcript_path` はパス情報だけをホーム部分を `~` に正規化して保存し、ファイルを開いて全文インポートすることはありません。dead-letter に落ちた場合も同じ正規化を適用します。transcript再取り込みは将来課題です。

`Cookie` / `Set-Cookie` ヘッダー、`private_key`、文字列として埋め込まれた JSON（`{"password":"..."}`）も置換対象です。`cwd` と `old_cwd` / `new_cwd` は検索に必要なため正規化せずそのまま保存します。

### 診断

```bash
./bin/agent-history claude-hook-status
./bin/agent-history claude-sessions
```

診断コマンドは Claude Code のバージョン、設定場所、導入状態、対象イベント、DB初期化状態、dead-letter件数、最近のエラー、最後に記録した Claude session を表示します。

### 重複防止の基準

Stage 2 のDB変更は破壊的な再作成ではなく、既存 `events` テーブルへ `source_event_id`、`payload_size`、`truncated`、`dedup_key` を追加する後方互換マイグレーションです。`BEGIN IMMEDIATE` と短いbusy retryにより、近接したHook書き込みでも session 内の `sequence_no` を採番します。

`dedup_key` は次の基準で作ります。

- **イベント固有IDがある場合**はそれを使います。`tool_use_id`（PreToolUse / PostToolUse / PostToolUseFailure / PermissionDenied）、`agent_id`（SubagentStart / SubagentStop）、`task_id`（TaskCreated / TaskCompleted）、`elicitation_id`（Elicitation / ElicitationResult）、`message_id`＋`index`（MessageDisplay）です。
- **`prompt_id` は使いません。** 公式スキーマ上これは「次のプロンプトまでの全イベントが共有するターン相関ID」であり、イベントIDとして扱うと 1 ターン内の FileChanged、Notification、SubagentStart、TaskCreated などが 1 件に潰れます。
- **固有IDがない場合**は、サニタイズ済みペイロード全体＋`prompt_id` のハッシュを使います。観測可能なフィールドが 1 つでも違えば別イベントとして保存されます。

この結果、同じ Hook を user と project の両スコープに登録して同一イベントが二重配信された場合は 1 件に統合され、同じ Bash コマンドの連続実行は `tool_use_id` が違うため別イベントとして残ります。**ただし、固有IDを持たないイベント（Notification、FileChanged など）が同一ターン内で完全に同一内容で発生した場合は 1 件に統合されます。**

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

- Codex CLI、ブラウザ会話、シェル履歴は自動収集しません。Claude Code は Stage 2 の Hooks連携で観測可能なイベントを自動記録できます。Hookを導入しない場合は従来どおり event を CLI から明示登録します。
- 日本語の形態素解析は導入していません。FTS5 の標準 tokenizer に LIKE 部分一致を併用するため、語単位ではなく部分一致になり、event 数が増えると LIKE 側の全表スキャンが遅くなります。
- 認証・認可、HTTP 受信 API、Web UI はありません。単一ユーザーのローカル利用を前提にしています。
- サニタイズは高信頼の正規表現ベースで、すべての秘密情報を保証するものではありません。保存前に内容を確認してください。
- ベクトル検索、AI 自動タグ付け、記事生成、provenance 管理は未実装です。

## 今後のロードマップ

### Stage 2

- Claude Code Hooks 連携（実装済み）
- Codex ログ収集
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
