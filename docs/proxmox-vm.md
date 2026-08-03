# Proxmox VM 運用

この手順は、agent-history を Proxmox VE 上の専用 KVM VM で恒久運用するためのものです。ctx、k3s、複数 VM、複数 project の横断収集は対象外です。

## 前提

- VM は Ubuntu 24.04 系を想定する
- 初期メモリは 6 GiB。Codex と Claude Code は同時起動しない
- VM への着信はホストからの SSH のみ、外向き HTTPS は許可する
- VM 内の利用者は `amida` 一人
- ホストのホームディレクトリ、Git worktree、Docker socket は VM へマウントしない

## Docker 前提

VM には rootful Docker Engine と Compose plugin を導入します。導入方法は
Ubuntu のパッケージ方針に合わせて選びますが、作業を進める前に次を確認します。

```bash
docker --version
docker compose version
sudo systemctl enable --now docker
sudo usermod -aG docker amida
```

`docker` グループへ追加した後は、`amida` でログアウト・ログイン（または
SSH の再接続）が必要です。rootful Docker を使うため、`amida` が daemon を
sudo なしで利用できることを確認してから進めます。

```bash
docker info
```

`docker info` が成功しない状態では Compose や systemd を登録しません。
Docker Engine、Compose plugin、`docker` グループが未導入の場合は、先に
VM の標準手順または Docker 公式パッケージ手順で導入してください。

## VM 内のディレクトリ

```text
/srv/agent-history/
├── workspace/agent-history/  # Git worktree と Compose 定義
├── data/                     # SQLite、spool、failed
│   └── spool/
│       ├── tmp/
│       ├── pending/
│       └── failed/
├── claude-home/              # 0700、認証情報を含む
├── codex-home/               # 0700、認証情報を含む
├── github-auth/              # 0700、認証情報を含む
└── backups/                  # SQLite online backup
```

初期ディレクトリは、VM 内で次のように作成します。

```bash
sudo install -d -o amida -g amida -m 0750 \
  /srv/agent-history/workspace
git clone https://github.com/noanoa117117/agent-history.git \
  /srv/agent-history/workspace/agent-history
cd /srv/agent-history/workspace/agent-history

sudo install -d -o root -g amida -m 0750 /etc/agent-history
sudo install -o root -g amida -m 0640 .env.vm.example \
  /etc/agent-history/agent-history-vm.env

# 数値を確認し、agent-history-vm.env の DEV_UID/DEV_GID を編集する。
id -u amida
id -g amida
sudoedit /etc/agent-history/agent-history-vm.env

sudo AGENT_HISTORY_VM_ENV_FILE=/etc/agent-history/agent-history-vm.env \
  ./scripts/agent-history-vm-init
```

`/etc/agent-history` は `0750 root:amida`、環境ファイルは `0640 root:amida`
にします。`DEV_UID` と `DEV_GID` は数値を直接記入し、`$(id -u amida)` のような
シェル展開は記入しません。init script が `amida` の実 UID/GID と一致することを
検証し、不一致なら理由と修正方法を表示して停止します。環境ファイルにはパスと
バージョンだけを置き、認証情報は記録しません。

## Compose

VM では named volume を使わず、VM 内 bind mount の override を使います。

```bash
cd /srv/agent-history/workspace/agent-history
docker compose --env-file /etc/agent-history/agent-history-vm.env \
  -f compose.yaml -f compose.vm.yaml config --quiet

# 初回または Dockerfile / lock 対象の更新時だけ明示的に build する。
docker compose --env-file /etc/agent-history/agent-history-vm.env \
  -f compose.yaml -f compose.vm.yaml build

# 通常起動では boot 時の暗黙 build を行わない。
docker compose --env-file /etc/agent-history/agent-history-vm.env \
  -f compose.yaml -f compose.vm.yaml up -d --no-build
```

`compose.vm.yaml` は named volume を使わず、すべての VM パスを long syntax の
bind mount として指定します。`bind.create_host_path: false` により、パスの
綴り間違いで空ディレクトリを自動作成せず、Compose が失敗します。利用中の
Compose plugin がこの指定を受け付けることを `config --quiet` で確認します。

`agent-history-dev` は Codex / Claude Code を手動起動する対話用コンテナ、`agent-history-worker` は常駐する単一 writer です。agent-shell が停止していても worker は独立して spool を取り込みます。

## VM 起動時の自動復帰

Docker が起動した後に Compose を起動する systemd ユニットを登録します。

```bash
sudo install -m 0644 systemd/agent-history-compose.service \
  /etc/systemd/system/agent-history-compose.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-history-compose.service
```

確認:

```bash
systemctl status agent-history-compose.service
docker compose --env-file /etc/agent-history/agent-history-vm.env \
  -f compose.yaml -f compose.vm.yaml ps
```

ユニットは `/srv/agent-history` の mount を `RequiresMountsFor` で要求し、
Docker の一時的な起動失敗時は 15 秒後に再試行します。systemd 起動時は
`up -d --no-build` だけを実行するため、image の build は上記の明示的な手順で
先に済ませます。`EnvironmentFile` の値は systemd と Compose の両方で読める
権限にしています。

VM 起動時に起動するのはコンテナと worker までです。Codex / Claude Code は SSH 接続後、必要な project で手動起動します。

## VM 上の Make 操作

VM の checkout では、通常の `make dev-start` や `make dev-worker-restart` を
実行すると named volume 構成を起動しないよう停止します。VM 用 target は明示的に
bind mount override と環境ファイルを選びます。

```bash
cd /srv/agent-history/workspace/agent-history
make vm-build
make vm-start
make vm-status
make vm-worker-restart
```

環境ファイルの場所を変更した場合は、次のように指定します。

```bash
make VM_ENV_FILE=/etc/agent-history/agent-history-vm.env vm-start
```

VM 用 `vm-purge` は意図的に無効です。既存の `purge` 保護も維持し、履歴・
spool・認証ディレクトリを Make から削除できないようにします。

通常運用では Compose を直接停止せず systemd 経由で操作するため、worker の
自動復帰状態を維持できます。`make vm-clean` はコンテナとネットワークを整理する
ための例外的な操作で、実行後に systemd ユニットが `active (exited)` のままになる
可能性があります。その場合は次のコマンドで復帰させます。

```bash
sudo systemctl restart agent-history-compose.service
```

## SQLite backup

Proxmox VM バックアップを主経路とし、SQLite backup は DB 単体の復旧・確認用に使います。実行中 DB を単純な `cp` でコピーせず、次の script を使います。

```bash
/srv/agent-history/workspace/agent-history/scripts/agent-history-db-backup
```

出力された DB に `PRAGMA integrity_check` を実行してから保存するため、WAL の付属ファイルを個別に扱う必要はありません。生成物は `backups/` に 0600 で置かれます。

第一段階の Proxmox バックアップは同じ物理ホスト上の保存先を暫定利用します。ホスト障害には耐えないため、後で別ディスク、NAS、Proxmox Backup Server などへ移します。

通常バックアップに認証情報を含めない方針を維持する場合は、将来、履歴データ用ディスクと認証ディレクトリ用ディスクを分離し、Proxmox 側で認証ディスクをバックアップ対象外にします。初期構築ではこの分離方法を確定してから完全 VM バックアップを有効化します。

## 受け入れ確認

1. VM を再起動し、systemd 経由で worker が復帰する。
2. Codex または Claude Code を SSH 後に起動する。
3. hook が `data/spool/pending/` にイベントを置く。
4. worker が SQLite へ取り込み、`session-list` と `search` で確認できる。
5. worker 停止中の pending を再起動後に取り込める。
6. `agent-history-db-backup` の生成 DB を別名で指定し、`session-list` と `search` が成功する。
