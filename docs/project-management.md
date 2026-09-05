# 複数プロジェクトの管理

`events` は観測可能な作業履歴の一次情報であり、プロジェクトごとに Markdown
へ複製しません。既存の `targets` / `session_targets` を repository の識別に使い、
現在状態だけを VM の `/srv/agent-history/project-state/<slug>/` に保存します。

## 配置と識別

各 repository は `/srv/agent-history/workspace/projects/<slug>` に clone します。
初回は GitHub 認証後、agent-shell から実行します。

```bash
cd /workspace/projects
git clone https://github.com/OWNER/REPOSITORY.git example-project
cd /workspace/agent-history
agent-history project-register \
  --slug example-project --name 'Example Project' \
  --root-path /workspace/projects/example-project
```

VMでは登録後、`make vm-project-codex PROJECT=example-project` または
`make vm-project-claude PROJECT=example-project` で、そのrepositoryをcwdとして起動できます。
sessionはhookのcwdから登録済みrepository targetへworker取込時に自動関連付けされます。

登録時に root path、`origin` remote、default/current branch、HEAD commit を取得し、
既存 SQLite の `repository` target を更新します。新規テーブルや追加 writer は使い
ません。remote のない local repository も登録できます。

Codex または Claude Code の session ID が分かる場合は、対象 project に明示的に
リンクします。これにより `agent-history search --target example-project QUERY` で
対象を絞れます。

```bash
agent-history project-link-session --slug example-project --session SESSION_ID
agent-history search 'deploy verification' --target example-project --context-before 1
```

## 現在状態の更新

`project.json` は機械可読な状態、`progress.md` は短時間で再開するための表示です。
どちらにも作業ログ本文は保存しません。更新コマンドは Git の branch/commit を再取得
し、次の構造を上書き生成します。

```markdown
# Project Progress

## Goal
## Current Status
## Completed
## Decisions
## Blockers
## Next Actions
## Verification
## Last Updated
```

```bash
agent-history project-update --slug example-project \
  --current-status 'VM deployment validation in progress' \
  --decision 'Keep SQLite events as the primary history' \
  --next-action 'Run the verified restore test'
agent-history project-show --slug example-project
```

`progress.md` はこの CLI が生成するため、手編集せず `project-update` で変更します。
SQLite の events を変更・削除する処理はありません。イベントから人間または AI が
summary 候補を作る場合も、検索結果を読んで更新案を作成し、上記コマンドで状態だけを
反映します。
