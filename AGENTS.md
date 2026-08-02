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
- そのため、FTS インデックスの同期テストを `search_events()` 経由で書いてはいけません。トリガーが壊れていても LIKE 側がヒットして通ってしまいます。`events_fts` を直接 MATCH して検証してください（`tests/test_events.py` 参照）。

## 変更時の注意

- 既存データを破壊しないでください。DB スキーマを変更するときは、既存 DB に対するマイグレーションを考慮します。
- SQLite DB 本体や秘密情報を Git に追加しないでください。
- 秘密情報をテストデータへ実値で入れないでください。テストにはダミー値、文書化用の予約ドメイン、RFC 5737 のドキュメント用 IP などを使います。
- ブラウザ拡張、自動収集、外部 AI 連携、記事生成は今回の範囲外です。

## テスト完了条件

変更後は、少なくとも以下を確認します。

```bash
python3 -m compileall src
bash -n bin/agent-history
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

DB 初期化、CLI による session/event/target 登録、FTS5 検索、Markdown export も実際に確認します。
