# AGENTS.md

## リポジトリの目的

`agent-history` は、Ubuntu 上で Codex、Claude Code、ブラウザ版 AI、人間のシェル操作などの観測可能な作業履歴を、SQLite に保存・検索するためのローカル CLI です。

## データ設計

- `sessions` と `events` を一次データとします。分類や検索用の補助データが不完全でも、生の観測可能なイベントを失わない設計にします。
- 作業単位を `project` や `root_path` に固定せず、session を中心にします。
- `targets` は repository、service、cloud resource、topic などを表し、1 セッションに複数関連付けできます。
- AI 内部の非表示な思考過程は保存対象にしません。保存するのは入出力、コマンド、ツール結果、メモ、エラーなどの観測可能な情報だけです。

## 検索とテストの注意

- 検索は FTS5 の MATCH と `events.content` の LIKE 部分一致の和集合です。LIKE は日本語のように分かち書きされない文を拾うためのフォールバックです。
- そのため、FTS インデックスの同期テストを `search_events()` 経由で書いてはいけません。トリガーが壊れていても LIKE 側がヒットして通ってしまいます。`events_fts` を直接 MATCH して検証してください（`tests/test_events.py`、`tests/test_claude_hooks.py` 参照）。

## Hook のクリティカルパス制約

- Hook は Claude Code のクリティカルパス上で動きます。`bin/agent-history-claude-hook` → `capture/hook_fast.py` → `capture/spool.py` の経路に **重い import を足さないでください**。実測で `json` は +7.6ms、`agent_history.sanitizer`（`re` を引く）は +24ms かかり、25ms の予算を壊します。`tests/test_spool.py` が実際にロードされたモジュールを検査して防いでいます。
- Hook は **SQLite を開かず、fsync せず、常に終了コード 0 を返します**。この3点は受け入れ条件そのものなのでテストで固定してあります。
- 重い処理（JSON 解析、サニタイズ、切り詰め、DB 書き込み）は全て `worker/` 側で行います。hook 側へ移さないでください。
- このリポジトリの検証環境は **回転ディスク**で、fsync 1回が約83msかかります。SSD で計測すると問題が見えなくなるので、性能判断は HDD 前提で行ってください。
- 詳細な設計と計測値は `docs/design/spool-worker.md` にあります。

## spool と worker の前提

- 収集は **best-effort** です。欠損許容を前提に、`processing/` による所有権管理・孤児回収・リトライ・バックプレッシャ優先度制御は**意図的に実装していません**。「堅牢にするため」にこれらを足す前に、その必要性を確認してください。
- 重複防止は既存の `UNIQUE(session_id, dedup_key)` に対する `ON CONFLICT DO NOTHING` だけで担保します。worker のクラッシュ時に `pending/` のファイルが再取り込みされる前提の設計です。
- worker は `flock` で単一に保ちます。複数 writer を許すと SQLite のロック競合が戻ってきます。
- バッチ commit（50件 or 1秒）が fsync 償却の要です。1イベント1トランザクションに戻さないでください。
- イベントプリセットは `presets.py` が単一の情報源です。`config/claude-hooks.*.json` は生成物で、`tests/test_worker.py` がドリフトを検出します。手で編集しないでください。

## Claude Hook の落とし穴

- `prompt_id` はイベントIDではなく「次のプロンプトまでの全イベントが共有するターン相関ID」です。重複防止キーに使うと、1ターン内の FileChanged / Notification / SubagentStart / TaskCreated などが 1 件に潰れます。イベント固有IDは `EVENT_IDENTITY_FIELDS` にイベントごとに定義してください。
- `agent_id` も、subagent 内で発火した全イベントが持つため、SubagentStart / SubagentStop 以外ではイベントIDになりません。
- Hook のテスト fixture は実際の Claude Code 入力スキーマに合わせてください。base スキーマの `session_id` / `transcript_path` / `cwd` / `prompt_id` が欠けた fixture は、上記のような不具合を隠します。
- イベント名と matcher 対象はローカル Claude Code バイナリの定義に固定してテストしています。Claude Code は未知のイベント名を警告付きで無視するだけなので、ずれても失敗が表面化しません。

## 変更時の注意

- 既存データを破壊しないでください。DB スキーマを変更するときは、既存 DB に対するマイグレーションを考慮します。
- SQLite DB 本体や秘密情報を Git に追加しないでください。
- 秘密情報をテストデータへ実値で入れないでください。テストにはダミー値、文書化用の予約ドメイン、RFC 5737 のドキュメント用 IP などを使います。
- Claude Code Hooks による観測可能なイベントのローカル自動収集は Stage 2 として実装済みです。Codex、ブラウザ、シェル履歴の自動収集、外部 AI 連携、記事生成は範囲外です。
- Claude Code のユーザー設定は `--apply` を指定したインストールコマンド以外で変更しません。Hookの失敗でClaude Codeをブロックせず、入力は既存サニタイザーを通してから保存します。
- DB スキーマを変更するときは、利用中の既存DBを削除せず、後方互換マイグレーションを追加します。

## テスト完了条件

変更後は、少なくとも以下を確認します。

```bash
python3 -m compileall src
bash -n bin/agent-history
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Claude Hook関連の確認には、次も実行します。

```bash
sh -n bin/agent-history-claude-hook
bash -n bin/agent-history-worker
bash -n scripts/install-claude-hooks
bash -n scripts/uninstall-claude-hooks
./scripts/install-claude-hooks
```

spool と worker を変更した場合は、実際に一巡させて確認します。

```bash
export AGENT_HISTORY_DB=/tmp/ah/test.db AGENT_HISTORY_SPOOL_DIR=/tmp/ah/spool
PYTHONPATH=src python3 -m agent_history init
echo '{"session_id":"t","cwd":"/tmp","hook_event_name":"Stop"}' | ./bin/agent-history-claude-hook Stop
PYTHONPATH=src python3 -m agent_history worker-drain
PYTHONPATH=src python3 -m agent_history claude-sessions
```

DB 初期化、CLI による session/event/target 登録、FTS5 検索、Markdown export も実際に確認します。
