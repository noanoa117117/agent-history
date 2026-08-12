---
+title: Codex CLI の公式 Hook を SQLite 作業履歴へつないだ話
tags:
  - SQLite
  - Python
  - Codex
  - ClaudeCode
private: false
updated_at: ''
id: null
organization_url_name: null
slide: false
ignorePublish: false
---

## TL;DR

- 先に作った `agent-history` の `spool → worker → SQLite` 基盤へ、Codex CLI の公式 Hook を接続した
- 現在記録するのは `SessionStart`、`UserPromptSubmit`、`Stop`、`SessionEnd` の4イベント
- Hook は stdin を spool ファイルへ置くだけ。SQLite を開かず、fsync も JSON 解析もサニタイズも Hook 側では行わない
- worker が後からサニタイズして SQLite / FTS5 へ取り込む。Claude Code の履歴と `source=codex` で同じテーブルに並ぶ
- Codex のツール実行生出力、rollout JSONL、認証情報、内部推論は読まず、保存しない

前回は、Claude Code の Hook が SQLite に直接書き込むせいで、HDD 環境では 1 回 268ms かかってしまった話を書いた。

その対策として作った `Hook → spool → worker → SQLite` の分離は、Claude Code 専用にしたくなかった。今回は Codex CLI の公式 Hook を同じ入力口へつなぎ、Claude と Codex の作業履歴を同じ SQLite で検索できるようにした話を書く。

なお、前回の **p50 16.5ms / p95 17.2ms は Claude Code Hook の実測値**である。Codex CLI の end-to-end レイテンシを別途ベンチマークした値ではない。本稿で扱うのは、Codex の記録経路と保存範囲である。

## 前提：保存先は1つ、入力元は複数

`agent-history` の一次データは `sessions` と `events` である。入力元ごとに DB やテーブルを分けず、セッションの `source` で区別する。

```
Claude Code ─┐
             ├─> Hook ─> spool ─> worker ─> SQLite / FTS5
Codex CLI ───┘                              │
                                               └─ source = claude-code / codex
```

この形なら、たとえば `Cloudflare Tunnel` を検索して、Claude Code で調べたログと Codex に実装を頼んだログを時系列で一緒に見られる。必要なら `source=codex` だけに絞ることもできる。

重要なのは、Hook の実装をエージェントごとに増やさないことだった。Hook は「受け取った stdin をあとで worker が読める場所へ置く」だけにし、JSON の解釈と SQLite 操作は共通 worker に寄せている。

## Codex で記録する4イベント

プロジェクト内の `.codex/hooks.json` で、次の4イベントを登録している。

| Hook | 保存する主な情報 | SQLite 上の event_type |
|---|---|---|
| `SessionStart` | session ID、model、cwd、開始元 | `session_start` |
| `UserPromptSubmit` | turn ID、prompt、model、cwd | `user_prompt` |
| `Stop` | turn ID、最終応答、cwd | `assistant_stop` |
| `SessionEnd` | session ID、終了理由、cwd | `session_end` |

設定は次のように、イベント名をコマンド引数として渡す。

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{
      "type": "command",
      "command": "/workspace/agent-history/bin/agent-history-codex-hook SessionStart",
      "timeout": 3
    }]}]
  }
}
```

実際の設定には4イベントすべてを登録している。イベント名を stdin の JSON から読まないのは意図的だ。Hook のクリティカルパスで `json` を import したくないためである。worker 側で、コマンド引数のイベント名と payload 内の `hook_event_name` が一致するかを検証する。

## Hook は SQLite に触らない

Codex 用の Hook は次の小さなシェルスクリプトから始まる。

```sh
exec python3 -S "${REPO_ROOT}/src/agent_history/capture/hook_fast.py" "$@" codex
```

末尾の `codex` は入力元を示す印で、共通の高速 Hook 実装に渡す。Hook が行うことは以下だけである。

1. stdin のバイト列を読む
2. `tmp/` に spool ファイルを作る
3. `os.replace` で `pending/` へ原子的に移動する
4. 常に終了コード 0 で戻る

SQLite 接続、fsync、JSON 解析、正規表現によるサニタイズ、イベント内容の切り詰めはすべて worker 側の仕事である。この分離は、前回の「Hook が遅い」問題への対策を、Codex 側でもそのまま使うためのものだ。

worker は spool をまとめて読み、1トランザクションで SQLite に保存する。途中で worker が落ちても、コミット済みになるまで spool ファイルは消さない。再取り込み時の重複は `UNIQUE(session_id, dedup_key)` と `ON CONFLICT DO NOTHING` で吸収する。

## 何を保存し、何を保存しないか

作業履歴といっても、Codex が内部で持つものを何でも収集するわけではない。保存対象は Hook が渡す観測可能なライフサイクル情報のうち、次の最小限に絞っている。

- セッション ID、turn ID
- ユーザープロンプトと最終応答
- cwd、model、開始元、終了理由

逆に、以下は読み取りも保存もしない。

- `transcript_path` と rollout JSONL
- Codex の認証情報・内部 SQLite・環境変数
- system / developer prompt
- 内部推論
- ツールの生出力

特に rollout JSONL を読めば過去履歴を多く取れそうに見えるが、それは Hook ベースのリアルタイム収集とは別の設計になる。形式への依存、保存範囲、再取り込み時の重複処理を改めて決める必要があるため、現時点の収集経路には含めていない。

worker は Codex の4イベントごとに許可するフィールドを定め、それ以外を落としてから既存サニタイザーへ渡す。たとえばテストでは、プロンプト中のメールアドレスは `<REDACTED_EMAIL>`、最終応答に含まれるトークン形式の文字列は `<REDACTED_SECRET>` へ置き換わることを確認している。

## 初回だけ Hook を確認して信頼する

project-local の Hook を初めて使うとき、または設定を変えたときは Codex TUI で `/hooks` を開く。表示された4件の Hook を確認してから、`t` で trust all を実行する。

これは実行権限を雑に迂回するためのものではない。Hook に登録されたコマンドが期待どおりのプロジェクト内スクリプトかを、人間が確認するための手順である。通常運用では hook trust を迂回するオプションは使わない。

## SQLite まで届くことをテストする

確認したかったのは「spool ファイルができる」ことだけではない。Codex の4イベントが worker を経由し、SQLite の `sessions` と `events` に正規化されるところまでをテストしている。

テストでは、`SessionStart` を重複配信したうえで残り3イベントを投入し、worker で drain する。結果は4件だけが挿入され、イベント種別は次の順になる。

```text
session_start  / system
user_prompt    / human
assistant_stop / codex
session_end    / system
```

同じイベントが worker のクラッシュ後などにもう一度届いても、二重記録にならないことまで確認できる。さらに Codex の spool ヘッダに `src=codex` が付くため、共通 worker が入力元を取り違えない。

## Claude Code と Codex は同じではない

共通基盤に乗せたからといって、両者の観測範囲が同じになるわけではない。

Claude Code 側は多くの lifecycle event を扱い、プリセットによって収集量を調整している。一方、Codex 側は今のところ4つの session / turn 境界イベントだけである。したがって、Codex の履歴は「そのセッションで何を頼み、最終的にどんな応答で終わったか」を追うための記録であり、全ツール呼び出しの監査ログではない。

これは不足というより、保存範囲を明示した設計判断である。何を拾えるかはエージェントごとの Hook が決める。spool と worker は、それぞれの入力を安全に共通の検索可能な履歴へ変換する層として使う。

## まとめ

- Codex CLI の4つの公式 Hook を、Claude Code と共通の spool / worker / SQLite 基盤へ接続した
- Hook は軽く保ち、重い処理と永続化は worker に分離した
- `source=codex` を持つため、同じ SQLite で横断検索と入力元ごとの絞り込みができる
- 保存範囲は prompt・最終応答・セッション境界に限定し、rollout、認証情報、内部推論、ツール生出力は収集しない
- Codex の専用レイテンシ値は未計測なので、Claude Code のベンチマーク値を流用しない

次は、こうして蓄積した履歴を MCP の検索ツールとして公開することを考えている。高頻度かつ受動的な収集は Hook、必要なときだけ過去の作業を引く読み出し口は MCP。この役割分担が、Claude Code と Codex の両方を扱うときにも自然だと思う。
