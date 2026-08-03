# Docker隔離開発環境

## 目的

この構成は、agent-historyの開発、テスト、Claude Code、GitHub操作をUbuntuホストから分離するためのものです。

8GBメモリのホスト上で大容量バイナリに対するstrings、grep、probe、テストが同時に走ると、メモリ、swap、ディスクI/Oが急増し、ホストのSSHやVS Code Remoteなどへ影響が広がる可能性があります。Dockerのcgroup制限で、Claude Codeが起動した子プロセスを含めてこの開発環境の資源を制限します。

GitHubを正本とし、ソースコードのGit作業ツリーはホストではなくagent-history-workspace named volumeへcloneします。ホストの/home/amida、~/.ssh、Dockerソケット、host networkはマウントしません。

## checkoutとworkspaceの役割

このリポジトリ内のDocker定義も、agent-history本体のコードと同じGitHub repositoryで管理します。Docker専用の別repositoryは作りません。

| 場所 | 役割 |
| --- | --- |
| ホスト側checkout | Docker環境の起動・更新用 |
| コンテナ側workspace | Claude Code、Codex CLI、テスト、コード編集、commit、pushを行う作業ツリー |
| GitHub | 正式な正本 |

ホスト側checkoutとコンテナ側workspaceは別のGit作業ツリーです。通常のagent-historyコード編集はworkspaceだけで行い、同じファイルをホスト側とコンテナ側で並行編集しません。

host-statusとhost-pullはホスト側checkout専用です。git-status、git-log、pull、pushはコンテナ側workspace専用です。

Composeにはproject名、image名、container名、volume名、network名を明示しています。そのため、ホスト側checkoutを別の場所へ再cloneしても、同じagent-history resourceを再利用できます。他プロジェクトでは別のproject/resource名を設定し、相互に共有しません。

## 前提条件

- Docker EngineとDocker Compose v2がホストに導入済みであること
- GitHubへアクセスできること
- 初回のClaude Code認証とGitHub CLI認証で、ホスト側ブラウザを使えること
- Dockerをsudoなしで操作できること（必要なら、ホスト管理者がDocker権限を別途設定する）

ホストのSSH、UFW、Cockpit、Cloudflare Tunnel、systemd、ネットワーク設定はこの構成では変更しません。

## 初回セットアップ

リポジトリのルートで実行します。

    cd /home/amida/projects/agent-history
    cp .env.example .env
    make dev-build
    make dev-start

make dev-host-statusで、ホスト側の薄いcheckoutの状態を確認できます。.envはGit管理対象外です。

ホスト側Docker定義をGitHubから更新する場合は、checkoutがcleanな状態で次を使います。

    make dev-host-pull

未コミット変更がある場合は停止します。Docker定義の変更はホスト側checkoutでcommit・pushし、その後に必要ならmake dev-buildを実行します。

.envはGit管理対象外です。URL、ブランチ、Claude Codeのバージョンを変更したい場合だけ編集します。UID/GIDはmakeのラッパーが現在のホストユーザーに合わせます。

次にGitHub認証とworkspaceの初期cloneを行います。

    make dev-gh-login
    make dev-gh-status
    make dev-pull
    make dev-git-status

make dev-pullはworkspaceが空ならcloneします。既存workspaceでは現在のブランチを対象にgit pull --ff-onlyだけを実行します。ブランチの切り替え、force、reset、clean、コンフリクトの自動解決は行いません。

ここでのmake dev-pullはコンテナ側workspaceだけを更新します。ホスト側checkoutの更新にはmake dev-host-pullを使います。

現在の既存運用でfeature branchを使う場合は、clone後にコンテナ内でブランチを作成・切り替えます。

    make dev-shell
    git switch -c <既存運用のブランチ名>

mainへの直接pushを構成の前提にはしていません。push先はworkspaceの現在ブランチとupstreamから解決し、実行前に表示します。

## Claude Code

コンテナを起動した状態で、次のコマンドを実行します。

    make dev-claude

引数を渡す場合は、例えば次のようにします。

    ./scripts/dev-container claude --continue
    make dev-claude CLAUDE_ARGS="--resume"

初回はClaude Codeがログインを促します。認証情報と設定、会話履歴はagent-history-claude-home volumeへ保存されます。Dockerfile、Compose、.env.example、Git管理対象へAPIキーやトークンを入れません。ホストの.claudeもマウントしません。

Claude Codeは公式Linux Native Installerでイメージへインストールします。初期バージョンはホストで確認した2.1.220です。更新時は.envのCLAUDE_CODE_VERSIONを変更してmake dev-buildを実行します。

既存のagent-history hookをコンテナ内Claude Codeへ適用する場合だけ、workspace内で次を実行します。設定変更先は専用Claude volumeで、ホスト設定は変更しません。

    ./scripts/install-claude-hooks --apply

会話ログを確認する必要がある場合は、次でコンテナ内から確認します。

    make dev-shell
    find "$CLAUDE_CONFIG_DIR" -maxdepth 3 -type f -print

認証情報の中身を表示・コピー・Gitへ追加しないでください。

公式のインストール要件とログイン手順は、[Claude Code installation docs](https://code.claude.com/docs/en/installation)と[Claude Code terminal guide](https://code.claude.com/docs/en/terminal-guide)を参照してください。

## Codex CLI

Claude Codeと同じ分離方針です。CLI本体はimage、認証情報はvolumeに置きます。コンテナ内で手動のnpm installは不要で、buildした時点から使えます。

    make dev-codex

通常のコード実行には、外部Docker隔離を明示する次のtargetを推奨します。リポジトリ内の`.codex/hooks.json`もこのworkspaceから自動的に読み込まれます。

    make dev-codex-external-sandbox

引数を渡す場合は次のようにします。

    ./scripts/dev-container codex --help
    make dev-codex CODEX_ARGS="exec 'summarize this repo'"

認証は次のコマンドです。初回のみブラウザ/デバイスログインを求められます。

    make dev-codex-login
    make dev-codex-status

### Codex公式hookの信頼

agent-historyはproject-localの`.codex/hooks.json`で`SessionStart`、`UserPromptSubmit`、`Stop`、`SessionEnd`を登録します。初回またはhook変更後は、Codex TUI内で`/hooks`を開き、4件のproject hookを確認して` t `で信頼してください。信頼状態はCodex専用named volumeの`$CODEX_HOME/config.toml`に保存され、Git管理しません。

Proxmox 上の恒久 VM での bind mount・systemd 自動起動・SQLite backup は [Proxmox VM 運用](proxmox-vm.md) にまとめています。

`--dangerously-bypass-approvals-and-sandbox`と`--dangerously-bypass-hook-trust`は別の設定です。本構成では前者だけを使用し、後者は通常運用で使用しません。

Codex hookは常にexit 0で戻ります。通常はstdoutを出さず、CodexがJSON応答を要求する`Stop`だけは中立な`{"continue":true}`を返します。stdinをspoolへ置くだけでSQLiteを開かず、fsyncもしません。workerだけがサニタイズとDB書き込みを行います。`auth.json`、Codex内部SQLite、環境変数、transcript/rolloutは対象外です。

Node.jsとCodex CLIはDockerfileのbuild時にインストールし、バージョンは.envのNODE_VERSIONとCODEX_VERSIONで固定します。Node.jsは公式バイナリ配布物をSHASUMS256.txtで検証して/usr/localへ展開し、Codex CLIはrootのままnpm install --globalします。これにより非rootユーザーは読み取り専用で利用でき、npmのグローバルインストール先による権限問題が発生しません。

    make dev-shell
    command -v node && node --version
    command -v npm && npm --version
    command -v codex && codex --version

認証情報と設定はagent-history-codex-home volume（コンテナ内の$CODEX_HOME = /home/amida/.codex）に保存されます。Dockerfile、Compose、.env.example、Git管理対象へAPIキーやトークンを入れません。ホストの~/.codexもマウントしません。dev-cleanでコンテナを作り直しても再ログインは不要です。

### コンテナ内ではCodex内蔵サンドボックスが動きません

Codexは`codex sandbox`やモデル生成コマンドの実行にbubblewrap（bwrap）を使い、これは非特権user namespaceを必要とします。本構成では次の2つが重なるため利用できません。

- ホストUbuntu 24.04の`kernel.apparmor_restrict_unprivileged_userns = 1`
- コンテナの`no-new-privileges: true`と`cap_drop: ALL`

次のエラーになります。

    bwrap: No permissions to create a new namespace, likely because the kernel does not allow non-privileged user namespaces.

**この構成ではコンテナ自体がサンドボックス境界です。** cgroupによるメモリ/CPU/PID制限、cap_drop、ホストパス非マウントで隔離しています。Codexにコマンドを実行させる場合は、外部サンドボックス環境向けに用意されている次のフラグを使います。

    make dev-codex CODEX_ARGS="--dangerously-bypass-approvals-and-sandbox"

このフラグは公式ヘルプで「Intended solely for running in environments that are externally sandboxed」と説明されているものです。ホストで直接使わないでください。セキュリティ姿勢を弱めてbwrapを通す（CAP_SYS_ADMIN付与など）ことはしません。

Codexが起動した子プロセスはコンテナのcgroup内に留まるため、メモリ3GB、PID 512の制限対象になります。

    docker top agent-history-dev -eo pid,comm
    cat /proc/<host-pid>/cgroup   # docker-<container-id>.scope 配下になる

公式のインストールと認証手順は、[Codex CLI docs](https://developers.openai.com/codex/cli)と[Codex auth](https://developers.openai.com/codex/auth)を参照してください。

## GitHubのcommit/push

GitHub認証はコンテナ専用です。推奨フローは次の通りです。

    make dev-start
    make dev-gh-login
    make dev-pull
    make dev-git-status
    make dev-shell
    git status
    git add <対象>
    git commit
    exit
    make dev-push

make dev-gh-loginはgh auth login --hostname github.com --git-protocol https --webを実行します。表示されたデバイスコードとURLをホストのブラウザで使ってください。認証情報、Git credential helper設定、GitHub CLI設定はagent-history-github-auth volumeへ保存されます。ホストの~/.sshやホストのGit認証情報には依存しません。

commit用の名前とメールアドレスは、必要ならコンテナ内で一度設定します。設定はGitHub認証volume内のglobal Git設定へ保存されます。

    make dev-shell
    git config --global user.name "Your Name"
    git config --global user.email "you@example.com"

make dev-pushは次を確認してから、通常のgit pushだけを実行します。

- Git repositoryであること
- detached HEADでないこと
- unresolved conflictがないこと
- upstreamが設定されていること
- 未コミット変更や未追跡ファイルがないこと
- 現在ブランチ、remote、remote URL（credential部分を除去）、ahead/behind、push対象commit、diff statを表示すること

force push用の引数や自動force設定はありません。認証失敗時はGIT_TERMINAL_PROMPT=0で停止し、トークン入力を促したり秘密情報を表示したりしません。

## 操作コマンド

    make dev-build       # イメージ作成
    make dev-start       # コンテナ起動
    make dev-host-status # ホスト側薄いcheckoutの状態
    make dev-host-pull   # ホスト側checkoutをff-only更新
    make dev-shell       # コンテナ内bash
    make dev-claude      # コンテナ内Claude Code
    make dev-codex       # コンテナ内Codex CLI
    make dev-codex-external-sandbox # 推奨: 外部Docker隔離 + 公式hook
    make dev-codex-login # Codexログイン
    make dev-codex-status # Codex認証状態
    make dev-worker-start   # spool workerサービス起動
    make dev-worker-stop    # 停止
    make dev-worker-restart # 再起動
    make dev-worker-status  # worker状態とspool件数
    make dev-worker-logs    # workerログ
    make dev-worker-drain   # 今ある分だけ取り込み
    make dev-gh-login    # GitHubデバイス/webログイン
    make dev-gh-status   # GitHub認証状態
    make dev-git-status  # branch/upstream/status
    make dev-git-log     # 最近のcommit
    make dev-pull        # 初回cloneまたはff-only pull
    make dev-push        # 安全確認付き通常push
    make dev-test        # 300秒のtimebox付きテスト
    make dev-logs        # Dockerログ（既定で末尾200行）
    make dev-status      # Compose状態
    make dev-stop        # 停止、volume保持
    make dev-clean       # コンテナ/network削除、volume保持
    make dev-purge       # 安全確認後に全volume削除

dev-purgeはworkspaceに未コミット変更、未追跡ファイル、未push commitがある場合に拒否します。安全確認を通過しても、PURGEの入力が必要です。認証、会話履歴、Git履歴、DB、ログ、テスト成果物を削除するため、通常はdev-cleanを使います。

## ホスト側checkoutの最小化と再clone

ホスト側checkoutはDocker環境の起動入口なので、Dockerfile、compose.yaml、Makefile、scripts、container、docsとGit管理情報があれば役割を果たします。agent-history本体の通常編集はworkspaceで行います。

ホスト側checkoutを再cloneする場合は、先に次を確認します。

    make dev-host-status
    docker compose ps

未コミット変更がなく、Docker操作を一時停止できる状態で、親ディレクトリへ別名cloneします。新しいcheckoutでmake dev-startを実行すると、明示されたagent-history image、container、volume、networkを再利用できます。GitHubにまだpushしていないDocker定義変更がある場合は、再clone前に失われないようcommit・pushまたはバックアップを行います。

workspace volumeはホストcheckoutとは独立しているため、ホストcheckoutの再cloneだけではworkspaceのコード、Git履歴、Claude/Codex/GitHub認証、SQLiteデータは削除されません。dev-purgeを実行した場合だけnamed volumeが削除されます。

## spool worker サービス

hookはイベントをspoolへ置くだけで、SQLiteへ入れるのはworkerです。**workerが動いていないとDBには反映されません**（spoolには貯まり続けます）。起動忘れを避けるため、workerはComposeの独立サービスとして常駐します。

    make dev-start          # agent-history-dev と agent-history-worker を一緒に起動

個別操作は次のとおりです。

    make dev-worker-start    # 起動
    make dev-worker-stop     # 停止（明示停止したら自動では起き上がりません）
    make dev-worker-restart  # 再起動
    make dev-worker-status   # Compose状態 + spool件数
    make dev-worker-logs     # ログ（既定で末尾200行）
    make dev-worker-drain    # 今ある分だけ取り込んで終了

### フォアグラウンド実行

Composeのcommandは`worker-start`ではなく`/usr/local/bin/agent-history-worker-run`です。`worker-start --detach`はフォークして親が終了するため、コンテナのPID 1が消えて`restart: unless-stopped`が再起動ループになります。

`agent-history-worker-run`は次の性質を持ちます。

- daemonizeしない
- `exec`でworker本体を起動するため、PID 1（docker-init）の直接の子になる
- SIGTERMをworker自身が受け取る
- 進捗をstderrへ逐次出力する（`docker compose logs`で追える）
- SIGTERM受信時は処理中バッチを完了してから終了する。バックログ全体をdrainしないため、Dockerのgrace period内に収まる。残りは次回起動時に取り込まれ、重複はdedupインデックスが吸収する

プロセスツリーは次のようになります。

    PID  PPID  COMMAND
      1     0  /sbin/docker-init -- /usr/local/bin/agent-history-worker-run
      7     1  python3 -u -m agent_history worker-run

### 単一writer

`data/spool/worker.lock`のflockで単一writerを保証します。lockファイルはagent-history-data volume上にあるため、devコンテナとworkerコンテナが同じlockを見ます。devコンテナ側で`worker-start`を実行すると、次のように拒否されます（終了コード1）。

    error: another agent-history worker is already running

常駐サービス側は、lockが取れない場合に終了せず待機します。終了すると再起動ループになるためです。

### DB初期化

workerは`depends_on`の起動順だけに依存しません。起動時にスキーマの有無を確認し、無ければ既存の冪等な初期化処理を実行します。FTSインデックスの再構築を毎回走らせないため、準備済みの場合は何もしません。

最大60秒待って準備できない場合は、理由をstderrへ出して終了します。無限に待ち続けて壊れたvolumeを隠すことはしません。

    agent-history-worker: database is not initialized; applying schema to ...
    agent-history-worker: database is ready

    error: database not ready after 60s: ... (PermissionError: ...)

### リソース制限とvolume

workerにはdevコンテナとは別の小さい制限を設定しています。

| 項目 | 値 |
| --- | --- |
| memory | 512MB |
| reservation | 256MB |
| memory+swap | 512MB（swap無効） |
| CPU | 0.5 |
| PID | 64 |
| cap_drop | ALL |
| no-new-privileges | true |
| init | true |
| restart | unless-stopped |
| ログ | 10MB × 3 |

volumeはdevコンテナと共有しますが、必要なものだけです。

| volume | worker |
| --- | --- |
| agent-history-workspace | read-only（コードを読むだけ） |
| agent-history-data | read-write（spoolとDB） |
| agent-history-claude-home | **マウントしない** |
| agent-history-codex-home | **マウントしない** |
| agent-history-github-auth | **マウントしない** |

workerは認証情報を必要としないため、認証volumeは渡しません。imageはdevと同一（`agent-history-dev:latest`）で、新規volumeも作りません。

### restart: unless-stopped の挙動

- ホストやDockerデーモンの予期しない再起動後、workerは復帰します
- workerプロセスが異常終了した場合も復帰します（`RestartCount`が増えます）
- `make dev-worker-stop`、`docker compose stop`、`docker compose down`で明示停止した場合は、デーモン再起動後も起動しません

## リソース制限

通常のdocker compose upで次の制限が有効になります。

| 項目 | 設定値 |
| --- | --- |
| メモリ上限 | 3GB |
| メモリ予約目安 | 2GB |
| RAM + swap合計 | 4GB |
| CPU | 2.0コア |
| PID数 | 512 |
| Dockerログ | 10MB × 3ファイル |
| privilege | 無効 |
| Linux capabilities | 全削除 |
| no-new-privileges | 有効 |
| network | Compose default bridge |
| init | init: true |

memswap_limitはコンテナのRAMとswapの合計値です。3GBのRAM上限に対し、合計4GBまでです。ホストのswap使用自体をゼロにはしないため、重い処理はtimeboxと出力制限を併用してください。

## 永続化

| volume | 保存内容 |
| --- | --- |
| agent-history-workspace | Git clone、作業ツリー、.git履歴 |
| agent-history-claude-home | Claude Codeの認証、設定、会話履歴 |
| agent-history-codex-home | Codex CLIの認証、設定、セッション履歴 |
| agent-history-github-auth | gh認証、Git credential helper、global Git設定 |
| agent-history-data | SQLite DB、data/spool/、テスト成果物 |

SQLiteの既定パスは/workspace/agent-history/data/agent_history.dbです。ソースコードとGit履歴はworkspace volume、実行データはdata volumeに分けています。

dev-stopとdev-cleanはvolumeを保持します。dev-purgeだけが5つのvolumeを削除します。コンテナの異常終了や再作成では、これらのvolumeの内容は削除されません。

## 大容量ファイルを扱うときのルール

時間制限なしで大容量バイナリ全体をstringsやgrepへ渡さないでください。出力件数、探索深さ、実行時間を制限します。

    make dev-shell
    run-timeboxed 60 strings large.bin | head -n 200
    run-timeboxed 60 rg -m 200 "pattern" path
    run-timeboxed 60 find path -maxdepth 3 -type f -print | head -n 500

パイプ全体をtimeboxする場合は、次の形式にします。

    run-timeboxed 60 bash -lc 'strings large.bin | head -n 200'

run-timeboxedはコマンドを独立したプロセスグループで起動し、timeout時にTERM、続いてKILLをグループ全体へ送ります。バックグラウンド化した処理を残さないでください。Claude Codeから起動された子プロセスも同じコンテナcgroup内に残るため、制限対象になります。

## メモリ不足の確認

コンテナ側を確認します。

    docker stats --no-stream agent-history-dev
    docker inspect agent-history-dev --format '{{json .HostConfig}}'
    docker inspect agent-history-dev --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}'
    docker compose ps
    docker compose logs --tail=200 agent-history-dev

OOMKilled=trueならコンテナ側のメモリ上限に達した可能性があります。Compose制限はホストへの影響を抑えますが、3GBコンテナ内の処理が必ず成功することは保証しません。

ホスト側は、ホストのSSHなどを変更せず読み取りだけで確認します。

    free -h
    swapon --show
    ps -eo pid,ppid,%mem,%cpu,stat,cmd --sort=-%mem | head -n 30
    journalctl -k -b 0 | grep -Ei 'oom|out of memory|killed process'

## メモリ増設後

24GBまたは32GBへ増設した後は、compose.yamlの次の値を見直します。

- mem_limit
- mem_reservation
- memswap_limit
- 必要に応じてcpus
- 必要に応じてpids_limit

変更後にmake dev-buildは必須ではありませんが、make dev-startでCompose設定を再作成し、docker inspectとdocker stats --no-streamで実効値を確認してください。ホストのSSH、VS Code Remote、Cockpit等へ十分な余裕を残してください。

ホスト側の常駐サービスに余裕を残す前提での変更目安は次の通りです。

| ホストメモリ | mem_limit | mem_reservation | memswap_limit | cpus | pids_limit |
| --- | --- | --- | --- | --- | --- |
| 24GB | 8g | 6g | 10g | 4.0 | 1024 |
| 32GB | 12g | 8g | 16g | 4.0 | 1024 |

実際の負荷を見ながら段階的に上げ、ホストのfree、swap、iowait、SSH応答を確認してください。

現在の3GB/2GB/4GB/2CPU/512 PIDは、8GBホストを守るための保守的な初期値です。Claude Code公式ドキュメントのLinux要件は4GB以上ですが、現在のコンテナ上限はホスト保護を優先して3GBにしています。重い処理でコンテナOOMになる場合は、まず入力・出力・timeboxを見直し、増設後にこの値を上げます。

## 完全削除

dev-purgeが削除するのは、Composeで定義した次のnamed volumeだけです。

- agent-history-workspace
- agent-history-claude-home
- agent-history-codex-home
- agent-history-github-auth
- agent-history-data

GitHub上のrepositoryやホストのソースコード、ホストの認証情報は削除しません。この構成ではホストのソースコードと~/.sshをそもそもマウントしていません。ただし、volume内の未push commit、未コミット変更、未追跡ファイルが検出された場合は削除を拒否します。
