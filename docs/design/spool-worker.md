# 設計: spool + 常駐ワーカーによる非同期イベント取り込み（スリム版）

対象ブランチ: `perf/claude-hook-overhead`

## 0. 前提となる計測値

環境: Ubuntu / ext4 / `sda` は **回転ディスク (`rotational=1`)** / Python 3.14

| 項目 | 実測 |
|---|---|
| 生の `fsync` 1回 | **83 ms** |
| **現行 hook 1回（全体）** | **268 ms** |
| └ `sqlite3.commit`（commit時 fsync） | 153 ms |
| └ `sqlite3.close`（WALチェックポイント時 fsync） | 66 ms |
| └ Python 起動 + import | 57 ms |

SQLite PRAGMA 別（実 ext4、1イベント INSERT）: `synchronous=FULL` 170.8 ms / `NORMAL`+`autockpt=0` 190.3 ms / `OFF` 0.9 ms。
`NORMAL` が効かないのは 1接続1プロセスのため **close 時に必ず WAL チェックポイントが走り fsync する** から。PRAGMA 調整では根治しない。

### hook の import コスト（`python3 -S`、20回平均）

| import | 実測 |
|---|---|
| `os`, `sys` のみ | **11.8 ms** |
| + `json` | 19.4 ms |
| + `agent_history.sanitizer`（`re` を引く） | 36.2 ms |

→ **hook は `os` と `sys` だけを import する。** JSON パースもサニタイズも worker 側で行う。

補足: `python3 -E` は `PYTHONPATH` を無視するため使わない。`-S`（site 抑制、6.6 ms 節約）のみ使う。

## 1. 方針

- **欠損許容**の best-effort 収集とする。堅牢化（`processing/` 所有権管理、孤児回収、リトライ/バックオフ、毒イベント分離、バックプレッシャ優先度制御）は**実装しない**
- hook は spool にファイルを1つ置いて終わる。fsync せず、SQLite を開かない
- worker は単一プロセスでバッチ取り込みし、fsync を償却する
- 受け入れ基準の hook p95 は **25 ms**（プロセス起動の下限が約11.8 ms のため 10 ms は達成不能）

## 2. spool データモデル

```
data/spool/
  tmp/          0700  書きかけ（worker は読まない）
  pending/      0700  取り込み待ち
  failed/       0700  壊れたイベントを移すだけ（リトライなし）
  worker.lock         flock による単一化
```

### ファイル名

```
{ts_ns:019d}-{pid}-{rand8hex}-{event}.spool
例: 1785652440916697000-23037-9f3c1a20-SessionStart.spool
```

19桁ゼロ埋めの ns により **辞書順 = 時系列順**。`pid` + 4バイト乱数で衝突回避。

### ファイル形式（1イベント1ファイル）

1行目にヘッダ、改行、以降が hook の stdin 生バイト列。

```
{"v":1,"uid":"...","ev":"SessionStart","ts_ns":1785652440916697000,"pid":23037,"size":412}
{"session_id":"...","hook_event_name":"SessionStart",...}
```

ヘッダは **hook が生成する安全な文字だけ**（数字・16進・ホワイトリスト済みイベント名）で構成するため、`json` を import せず文字列組み立てで生成できる。
payload に改行が含まれても、worker は「最初の改行より後ろ全部」を payload として扱うので問題ない。

| 項目 | 方針 |
|---|---|
| 一意性 | `ts_ns + pid + 4バイト乱数`。`tmp/` へ `O_CREAT\|O_EXCL` で作成し衝突を検出 |
| event_id | `uid`（= ファイル名 stem）。DB の `source_event_id` とは別 |
| session_id | payload 内。worker が解決 |
| timestamp | `ts_ns`（並び順）。DB の `occurred_at` は worker が生成 |
| schema_version | ヘッダ `v`。worker は `v != 1` を `failed/` へ |
| 最大サイズ | stdin を 1 MB で打ち切る。超過分は捨てて `size` に実サイズを記録 |
| 途中書き込み | `tmp/` に書き `os.replace` で `pending/` へ。`pending/` は常に完全 |
| 破損 JSON | `failed/` へ移動。バッチは止めない。リトライしない |
| 上限 | `pending/` が 50,000 件を超えたら hook は書き込みをやめる（1/128 サンプリングで検査、早期打ち切り）。ディスクを埋めないための唯一のガード |
| パーミッション | ディレクトリ 0700 / ファイル 0600、`umask 077` |

### サニタイズの責務

**worker 側に一本化する。** hook で行うと import だけで 36 ms かかり予算を超えるため。

spool は DB 本体と同じ機密度として 0700/0600 で保護し、worker 稼働中の滞留時間は 1 秒未満。この点は README に明記する。

## 3. worker

- `worker.lock` に `flock(LOCK_EX|LOCK_NB)`。取得失敗＝二重起動で exit 3
- `pending/` を辞書順に最大 **50 件 or 1 秒**でバッチ化
- 各ファイル: ヘッダ検証 → payload を JSON パース → 既存 `_prepare_payload` / `_content` / `_dedup_key` を再利用 → 恒久エラーは `failed/` へ
- **1 トランザクションで全件 INSERT**。FTS は既存トリガ `events_fts_ai` が同一トランザクション内で更新するため追加実装不要
- `sequence_no` はバッチ内でセッションごとに `MAX` を1回取得してメモリ採番
- commit 成功後にファイル削除。クラッシュ時は `pending/` に残り次回再取り込みされるが、重複は下記で防ぐ
- SQLite 設定は `journal_mode=WAL` / `synchronous=FULL` / `busy_timeout=5000` を維持（`OFF` は採用しない）
- 接続は開きっぱなしにする。現行の「close 毎の WAL チェックポイント」（66 ms）が消える
- SIGTERM: 現バッチを終えてから終了する。処理中に強制終了されてもファイルが `pending/` に残るだけ

### 重複防止

既存スキーマの `UNIQUE(session_id, dedup_key)` に対し `INSERT ... ON CONFLICT DO NOTHING` を使う。
現行の「事前 SELECT」方式より短く、バッチ内の自己重複にも強い。**堅牢化ではなく単純化**。

### 順序保証

- 保証する: 同一セッション内で `sequence_no` はファイル名（hook が stdin を読み終えた時刻）の昇順
- 保証しない: hook は並行起動されうるため「論理的な発生順」とは限らない。複数セッション間の相対順序。壁時計巻き戻し時の単調性

## 4. durability

- hook のクリティカルパスで **fsync しない**
- OS クラッシュ・電源断で **直前の数秒のイベントが失われうる**
- SQLite 本体は `synchronous=FULL` を維持し、DB ファイルの整合性は保たれる
- `agent-history` は Claude Code をブロックしない best-effort な履歴収集である

以上を README に明記する。

## 5. イベントプリセット

| preset | イベント | 想定頻度 |
|---|---|---|
| `minimal` | `SessionStart` `SessionEnd` `UserPromptSubmit` `Stop` | 数件/ターン |
| **`balanced`（既定）** | minimal + `StopFailure` `Notification` `PermissionRequest` `PermissionDenied` `SubagentStart` `SubagentStop` `TaskCreated` `TaskCompleted` `PreCompact` `PostCompact` `CwdChanged` `ConfigChange` `InstructionsLoaded` `Setup` `UserPromptExpansion` `Elicitation` `ElicitationResult` `TeammateIdle` `DirectoryAdded` `WorktreeRemove` | 十数件/ターン |
| `full` | balanced + `PreToolUse` `PostToolUse` `PostToolUseFailure` `PostToolBatch` `FileChanged` `MessageDisplay` | 数百〜数千件/ターン |

`WorktreeCreate` は既存方針どおり全プリセットで登録しない。
`full` は明示 opt-in。回転ディスク検出時は install で警告する。

インストール時、各イベントの `command` に **イベント名を引数として埋め込む**（`... /bin/agent-history-claude-hook SessionStart`）。
これにより hook は JSON をパースせずにイベント名を知れる。worker が payload の `hook_event_name` と突き合わせて検証する。

### 申し送り

`PreToolUse` / `PostToolUse` はツール実行履歴という本プロジェクトの中核データだが、ご指示どおり `full` のみに置く。
spool 化後は 1 イベント約 12 ms なので、実測確認後に `balanced` へ昇格するか改めて判断されたい。

## 6. 運用 CLI

`worker start` / `worker stop` / `worker status` / `worker drain` / `spool status` / `failed list` / `failed purge`。
`failed retry` は「リトライしない」方針のため実装しない。

Docker 対応は未着手のため、まずローカル単体で動く構成を優先する。

## 7. テスト（14本）

| # | テスト |
|---|---|
| 1 | hook が `sqlite3.connect` を呼ばない（受け入れ基準1の直接検証） |
| 2 | hook が `os.fsync` を呼ばない（受け入れ基準3の直接検証） |
| 3 | hook の p50/p95 測定（閾値 25 ms） |
| 4 | 32 プロセス同時起動でファイル名衝突なし・全件が `pending/` に揃う |
| 5 | spool 書き込み不能でも hook が exit 0 |
| 6 | `tmp/` の書きかけを worker が読まない |
| 7 | ディレクトリ 0700 / ファイル 0600 |
| 8 | 100 件投入 → 複数バッチで全件反映、commit 回数が 100 未満 |
| 9 | 同一ファイルを2回処理しても events が増えない（`ON CONFLICT`） |
| 10 | 破損 JSON が `failed/` へ隔離され、正常分は取り込まれる |
| 11 | 同一セッション内の `sequence_no` がファイル名順と一致 |
| 12 | 既存6件の DB が改変されない |
| 13 | `count(events) == count(events_fts)` かつ `events_fts` を直接 MATCH して検証 |
| 14 | プリセット定義（`full ⊃ balanced ⊃ minimal`、高頻度5種は `full` のみ、`WorktreeCreate` は全プリセットに無い） |

加えて既存 50 テストが通ること。

## 8. 既存DBとの互換性

**スキーマ変更なし。** `sessions` / `events` / `events_fts` / `UNIQUE(session_id, dedup_key)` をそのまま使う。
`migrate_schema()` への追加なし。既存6件は無変更。既存 CLI の挙動も不変。

## 9. 実装ステップ

| Step | 内容 |
|---|---|
| 1 | `capture/spool.py`（`os` のみ import）+ `capture/hook_fast.py` + `bin/` ラッパ更新 → **p95 を実測して 25 ms 以内を確認** |
| 2 | `worker/ingest.py`: バッチ取り込み、`ON CONFLICT`、`sequence_no` バッチ採番 |
| 3 | `worker/runner.py`: flock、ループ、SIGTERM |
| 4 | `presets.py` + `install --preset` + イベント名引数の埋め込み |
| 5 | `worker-*` / `spool-*` CLI |
| 6 | テスト14本 |
| 7 | README / AGENTS.md 更新 |
