---
title: Claude Code のHookで作業履歴を全部SQLiteに貯めようとしたら、Claude Code 本体が重くなった話（268ms → 17ms）
tags:
  - SQLite
  - Python
  - ClaudeCode
  - パフォーマンス
  - AI
private: false
updated_at: ''
id: null
organization_url_name: null
slide: false
ignorePublish: false
---

## TL;DR

- AI との作業履歴をローカル SQLite に貯める CLI（`agent-history`）を作り、Claude Code の Hook で自動収集するようにした
- 実環境で有効化したら **Claude Code が体感でわかるほど重くなった**
- 計測したら Hook 1 回が **268ms**。うち `commit` の fsync が 153ms、`close` 時の WAL チェックポイントが 66ms
- 原因は「開発機のディスクが HDD で、**生の `fsync` が 1 回 83ms** かかる」ことだった
- `PRAGMA synchronous` の調整では直らない。**書き込みをクリティカルパスから外す**しかなかった
- Hook は spool ファイルを 1 個置くだけにして、常駐 worker がバッチで SQLite に入れる 2 段構成へ変更 → **p95 17ms**
- ただし当初目標の **p95 10ms は物理的に達成できなかった**。この機械では**プロセス起動そのものが 11.8ms** かかるため

計測値はすべて自分の開発機（Ubuntu / ext4 / HDD / Python 3.14）での実測です。SSD なら話はまったく違います。そこも含めて書きます。

---

## 何を作っていたか

`agent-history` は、AI と人間の作業履歴をローカルの SQLite に貯めて FTS5 で検索する CLI です。動機は単純で、

- Claude Code で解決した問題を、3 週間後にまた同じように調べ直している
- 「あのとき Cloudflare Tunnel の設定どうやったっけ」が、ターミナルのスクロールバックにも Git 履歴にも残っていない
- 記事を書こうとすると、作業の経緯が思い出せない

という状態を何とかしたかった、というものです。

設計で 1 つだけ最初に決めたのは、**一次単位を `project` ではなく `session` にする**ことでした。1 回の作業は複数のリポジトリ、ホスト、クラウドリソースにまたがりますし、Cloudflare や Azure のようにローカルディレクトリを持たない対象もあります。ローカルパスに紐づけると、そもそも入り口で分類に失敗します。

```
sessions        ── 作業の時間的まとまり（一次単位）
events          ── prompt / コマンド / ツール結果 / エラー など観測可能な入出力
targets         ── repository / service / cloud resource など作業対象
session_targets ── 多対多。分類に失敗しても events は失われない
events_fts      ── events.content の FTS5 インデックス（トリガーで同期）
```

保存するのは**観測可能な入出力だけ**です。AI の内部推論や非表示の思考過程は保存対象にしていません。

Stage 1 は CLI から手で `session-start` / `event-add` する形で動いていました。当然、手で登録するものは続きません。そこで Stage 2 として、**Claude Code の Hook から自動収集する**ことにしました。

## Stage 2: Claude Code Hooks で自動収集する

Claude Code には、ライフサイクルの各所で外部コマンドを起動する Hook の仕組みがあります。`~/.claude/settings.json` にこう書くと、

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/amida/projects/agent-history/bin/agent-history-claude-hook UserPromptSubmit"
          }
        ]
      }
    ]
  }
}
```

イベント発生時に stdin から JSON が渡ってきます。これを受けて SQLite に INSERT すればいい。実装としては素直です。

手元の Claude Code（`2.1.220`）が持つ Hook イベント定義は 31 種類ありました。`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Notification` / `Stop` / `PreCompact` …… せっかくなので全部拾う設定を書き、`--apply` してインストールしました。

そして Claude Code を再起動して、使い始めて、10 分でアンインストールしました。

## 「なんか重い」から始まる

体感として、プロンプトを送ってから反応が返るまでのもたつき、ツール実行のたびの引っかかりが明確にありました。「なんか重い」は主観なので、まず計測しました。

Hook 1 回を丸ごと計測すると、こうなりました。

| 項目 | 実測 |
|---|---|
| **Hook 1 回（全体）** | **268 ms** |
| └ `sqlite3.commit`（commit 時 fsync） | 153 ms |
| └ `sqlite3.close`（WAL チェックポイント時 fsync） | 66 ms |
| └ Python 起動 + import | 57 ms |

そして、そもそもこの機械の生の `fsync` は **1 回 83ms** でした。

```bash
$ cat /sys/block/sda/queue/rotational
1
```

**HDD でした。** 開発機のシステムディスクが回転ディスクであることを、このとき初めて意識しました。SSD なら fsync は 1ms 前後なので、まったく問題にならなかったはずです。逆に言えば、SSD 前提で書いたコードは HDD で 100 倍遅くなる箇所を平気で持ちます。

ここに、**約 30 種類のイベント全部に Hook を登録した**設定が乗ります。`PreToolUse` / `PostToolUse` / `FileChanged` / `MessageDisplay` あたりは 1 ターンで数百回発火します。Claude Code のクリティカルパス上で、268ms が数百回、直列に。重くて当然でした。

## PRAGMA では直らない

最初に考えたのは当然 `PRAGMA synchronous` です。計測しました（実 ext4、1 イベント INSERT）。

| PRAGMA | 実測 |
|---|---|
| `synchronous=FULL` | 170.8 ms |
| `synchronous=NORMAL` + `wal_autocheckpoint=0` | 190.3 ms |
| `synchronous=OFF` | 0.9 ms |

**`NORMAL` にしても改善しない**（むしろ誤差で悪化している）のが重要な点でした。理由は、この Hook が **1 回の起動につき 1 接続を開いて閉じる**プロセスだからです。SQLite は接続の close 時に WAL チェックポイントを走らせ、そこで fsync します。`synchronous=NORMAL` は commit 時の fsync を省く設定であって、チェックポイントの fsync は消えません。プロセスが毎回死ぬ以上、close は必ず来ます。

`OFF` にすれば 0.9ms です。しかし、**履歴を貯めるツールが DB ファイルを壊すのは本末転倒**なので、これは採りませんでした。`synchronous=OFF` は電源断で DB そのものが壊れうる設定です。「イベントを数件失う」と「DB が壊れる」はまったく違う障害クラスです。

つまり、**PRAGMA の調整では根治しない**。fsync をクリティカルパスから物理的に外すしかない、という結論になりました。

## 設計変更: spool + 常駐 worker

やったことは、要するに書き込みの非同期化です。

```
Claude Code ──> bin/agent-history-claude-hook ──> data/spool/pending/  (約12ms, fsync なし)
                                                          │
                          agent-history worker-start ─────┘
                                                          ↓
                                        50件 or 1秒ごとに 1 トランザクション ──> SQLite
```

- **Hook**: stdin を読んで spool ファイルを 1 個置き、即座に終了する。SQLite を開かない。JSON を解析しない。サニタイズもしない
- **Worker**: 単一プロセス（`flock` で保証）が spool をバッチ取り込みする。JSON 解析、サニタイズ、切り詰め、FTS 更新は全部こちら

バッチ化により、fsync は最大 50 イベントで 1 回に償却されます。Hook 側の fsync はゼロです。

spool ファイルの名前はこうしました。

```
{ts_ns:019d}-{pid}-{rand8hex}-{event}.spool
例: 1785652440916697000-23037-9f3c1a20-SessionStart.spool
```

19 桁ゼロ埋めのナノ秒にすることで、**辞書順 = 時系列順**になります。worker は `scandir` してソートするだけで済み、ファイル名から順序を決められます。`pid` + 4 バイト乱数で衝突を避け、`tmp/` へ `O_CREAT|O_EXCL` で作ってから `os.replace` で `pending/` へ原子的に移動します。これで書きかけのファイルが読まれることはありません。

## Hook を痩せさせる — import が高い

さて、spool に置くだけにしたので速くなったはず……と思って計測したら、まだ 30ms 台でした。今度は fsync ではなく **Python の import** が支配的になっていました。

`python3 -S` で 20 回平均を取るとこうです。

| import | 実測 |
|---|---|
| `os`, `sys` のみ | **11.8 ms** |
| + `json` | 19.4 ms |
| + `agent_history.sanitizer`（`re` を引く） | 36.2 ms |

**`json` を import するだけで 7.6ms、サニタイザ（実体は `re`）で 24ms。** 25ms の予算に対して、これは乗せられません。

なので、Hook からこの 2 つを追い出しました。

### 1. JSON を解析しないでイベント名を知る

Hook はイベント名を知る必要があります（ファイル名に入れるため）。しかし payload の `hook_event_name` を読むには `json` が要る。

そこで、**インストーラが登録するコマンドにイベント名を引数として埋め込む**ようにしました。

```json
"command": ".../bin/agent-history-claude-hook SessionStart"
```

Hook は `argv[1]` を見るだけです。payload の値との突き合わせ検証は worker 側でやります。

### 2. JSON を書かないで JSON ヘッダを作る

spool ファイルの 1 行目にはメタ情報のヘッダを置きたい。しかし `json.dumps` は使えない。

これは、**エスケープが必要な値を一切ヘッダに入れない**ことで解決しました。

```python
def build_header(uid, event_name, size, pid=None, truncated=False, source=None):
    """Build the envelope header line without importing `json`.

    Every interpolated value is machine-generated and restricted to digits,
    hex, or `safe_label` output, so no JSON escaping is required.
    """
    header = '{"v":%d,"uid":"%s","ev":"%s","ts_ns":%d,"pid":%d,"size":%d,"truncated":%s' % (
        SCHEMA_VERSION, uid, safe_label(event_name),
        int(uid.split("-", 1)[0]), pid, size,
        "true" if truncated else "false",
    )
    ...
```

`safe_label` は英数字と `_.-` 以外を `_` に潰す関数で、これも `re` を使わずに書いてあります。

```python
def safe_label(value, limit=64):
    """Reduce a value to filename-safe characters without importing `re`."""
    if not value:
        return "unknown"
    out = []
    for character in str(value)[:limit]:
        if character.isalnum() or character in "_.-":
            out.append(character)
        else:
            out.append("_")
    return "".join(out) or "unknown"
```

ヘッダに入るのは数字・16進・ホワイトリスト済みのイベント名だけなので、文字列連結で正しい JSON になります。payload（生の stdin）は改行の後ろに丸ごと置き、worker は「最初の改行より後ろ全部」を payload として扱います。payload 自体に改行が含まれていても問題ありません。

なお `python3 -E` は `PYTHONPATH` を無視してしまうので使えず、`-S`（site 抑制、6.6ms 節約）だけ使っています。

### 3. spool 溢れのチェックをサンプリングする

worker を起動し忘れると `pending/` が無限に増えてディスクを埋めます。これだけはガードしたい。しかし毎回 `scandir` すると、それこそコストです。

なので **1/128 の確率でしかチェックしません**。

```python
def should_check_pending(sample=PENDING_CHECK_SAMPLE):
    return os.urandom(1)[0] < max(1, 256 // sample)
```

上限（50,000 件）を超えたら Hook は書き込みをやめます。カウントも `cap` に達した時点で早期打ち切りします。「ディスクを埋めない」という 1 点だけを守るガードで、それ以外の堅牢化は入れていません。

## 結果と、達成できなかった目標

| | 変更前 | 変更後 |
|---|---|---|
| Hook レイテンシ (p95) | 268 ms | **16.8〜18.5 ms** |
| Hook が SQLite を開く | はい | いいえ |
| クリティカルパス上の fsync | 2 回 | **0 回** |

実測は p50 16.5ms / p95 17.2ms（コンテナ内 200 回）です。

**ただし、当初の目標だった p95 10ms は達成できていません。** そして、これはチューニングで届く距離ではありませんでした。この機械では、

- Python の `os` と `sys` だけの起動で **11.8ms**
- `cat` と `mv` だけの `/bin/sh` スクリプトでも **11.4ms**

つまり **fork + exec 型の Hook である限り、10ms は物理的に到達不能**です。Python が遅いのではなく、プロセス起動が 11ms かかる環境なのです。

そこで受け入れ基準を **p95 25ms 未満**に改めました。10ms 未達を許容した根拠は 2 つです。

1. 268ms からの改善幅が十分に大きい
2. **クリティカルパス上の SQLite オープンと fsync がどちらもゼロになった**。残っているコストはプロセス起動が支配的で、**ディスク性能に依存しない**

2 番目が重要でした。「速くなった」だけでなく、「**遅いディスクでも遅くならない構造になった**」ことが本質だと考えています。ベンチマークの数字が良くなったかではなく、**その数字が何に依存しているか**が変わったかどうかが、この手の改善の本当の合否だと思います。

## 何を捨てたか（重要）

速さの代償として、明示的に捨てたものがあります。README にも書いています。

> **agent-history は Claude Code をブロックしない best-effort な履歴収集ツールです。イベントの取りこぼしが起こりえます。**

具体的には、

- **Hook は fsync を一切しない**ので、OS クラッシュや電源断で**直前の数秒間のイベントが失われうる**
- worker を起動していない間も spool には貯まるが、上限を超えたら新しいイベントは捨てる
- 壊れたイベントはリトライせず `failed/` に隔離するだけ

一方で、**SQLite 本体は `synchronous=FULL` を維持**しています。DB ファイルの整合性は保たれます。ここは譲りませんでした。

`processing/` ディレクトリでの所有権管理、孤児回収、リトライとバックオフ、毒イベントの分離、バックプレッシャの優先度制御 — こういう「ちゃんとしたキュー」の機能は**意図的に実装していません**。履歴収集ツールにとって、イベントを 3 件失うことよりも、複雑さを抱えて動かなくなることのほうが致命的だからです。

順序についても、保証するのは 1 つだけにしました。

- **保証する**: 同一セッション内で `sequence_no` が spool ファイル名（Hook が stdin を読み終えた時刻）の昇順になる
- **保証しない**: 「イベントが論理的に発生した順」であること。Hook は Claude Code から並行に起動されうるため。複数セッション間の相対順序。NTP 調整で壁時計が巻き戻った場合の単調性

書けもしない保証を README に書かないことのほうが、実装するより大事だと思っています。

## クラッシュしても壊れない仕組み

捨てたものが多い代わりに、壊れないことは構造で担保しました。

- spool ファイルは `tmp/` に書いてから `os.replace` で `pending/` へ移す → **書きかけが読まれることはない**
- worker はコミット成功**後**にファイルを削除する → その間にクラッシュしてもファイルは `pending/` に残り、次回再取り込みされる
- 再取り込みによる重複は `UNIQUE(session_id, dedup_key)` と `INSERT ... ON CONFLICT DO NOTHING` が吸収する

「リトライしない」のに「クラッシュしても失われない」のは、削除の順序と冪等性だけで成立しています。リトライ機構を書くより、こちらのほうが短くて確実でした。

## おまけ: dedup_key で踏んだ罠

重複防止のキーをどう作るかで 1 つ罠がありました。

Claude Code の payload には `prompt_id` という ID が入っています。一見これがイベント ID に見えます。しかし公式スキーマ上、これは「**次のプロンプトまでの全イベントが共有するターン相関 ID**」でした。

これをイベント ID として使うと、1 ターン内の `FileChanged`、`Notification`、`SubagentStart`、`TaskCreated` などが**全部 1 件に潰れます**。

なので、

- **イベント固有 ID があればそれを使う**: `tool_use_id`（PreToolUse / PostToolUse など）、`agent_id`、`task_id`、`elicitation_id`、`message_id`＋`index`
- **固有 ID がなければ**、サニタイズ済みペイロード全体 + `prompt_id` のハッシュ

としました。これで、同じ Bash コマンドを連続実行しても `tool_use_id` が違うので別イベントとして残ります。逆に、同じ Hook を user と project の両スコープに登録して二重配信された場合は 1 件に統合されます。

（固有 ID を持たないイベントが同一ターン内で完全に同一内容で発生した場合は 1 件に統合されてしまいます。これは許容しました。）

## おまけ 2: 全イベント収集はやめた

性能問題が直ったので全 30 イベントを収集してもよくなった……とはなりませんでした。`full` は 1 ターンで数百〜数千イベント出ます。1 イベント 17ms でも、500 イベントなら 8.5 秒です。

そこでプリセットを用意し、既定を `balanced` にしました。

| preset | イベント数 | 想定頻度 |
|---|---|---|
| `minimal` | 4 | 数件/ターン |
| **`balanced`（既定）** | 24 | 十数件/ターン |
| `full` | 30 | 数百〜数千件/ターン |

`full` のみに含まれる高頻度イベント: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `FileChanged`, `MessageDisplay`

`full` は明示的な opt-in にして、回転ディスクを検出したらインストール時に警告を出すようにしました。今回の教訓をそのままツールに埋め込んだ形です。

なお `balanced` にはツール実行履歴（`PreToolUse` / `PostToolUse`）が入りません。本プロジェクトの中核データではあるので、実運用の実測を見て `balanced` へ昇格させるかを後で判断する、という宿題にしています。

## おまけ 3: 日本語全文検索で FTS5 だけでは足りない

本筋ではないですが、同じプロジェクトで踏んだのでメモとして。

FTS5 標準の `unicode61` tokenizer は日本語を分かち書きしません。なので `CockpitをCloudflare Tunnelで公開したい` は 1 トークン扱いになり、`MATCH 'Cockpit'` ではヒットしません。

現状は **FTS5 の MATCH と `LIKE` 部分一致の和集合**でごまかしています。副作用として、

- 語の途中でもヒットする（`cat` が `concatenate` にヒットする）
- **LIKE 側は全表スキャン**なので、event 数が増えると遅くなる

FTS5 の `trigram` tokenizer 導入が次の改善候補です。

## まとめ

- **「なんか重い」は必ず計測する。** 犯人は Python でもサニタイズでもなく、HDD の fsync 83ms でした
- **`PRAGMA synchronous=NORMAL` は、1 起動 1 接続のプロセスでは効かない。** close 時の WAL チェックポイントで結局 fsync するからです
- **クリティカルパス上の処理は、import 1 つが 7.6ms として効く。** `json` すら重い世界がある
- **プロセス起動には下限がある。** fork + exec 型の Hook で 10ms を切るのは、この機械では不可能でした。目標のほうを実測に合わせて改めるべき場面がある
- **速くなったかより、何に依存しなくなったかを見る。** ディスク性能に依存しないところまで持っていけたことが、この改善の本体です
- **履歴収集ツールに完璧な配送保証は要らない。** 捨てるものを決めて README に明記するほうが、実装するより価値がありました

同じ構成（Hook + ローカル DB）を考えている方は、まず `cat /sys/block/<dev>/queue/rotational` を見てください。そこで話が半分決まります。
