# Proxmox VM 運用

この手順は、agent-history を Proxmox VE 上の専用 KVM VM で恒久運用するためのものです。ctx、k3s、複数 VM、複数 project の横断収集は対象外です。

## 前提

- VM は Ubuntu 24.04 系を想定する
- vCPU は 4、RAM はホストの空きと既存 VM の予約を確認して決める（初期目安 16 GiB）
- system disk は 32 GiB 以上、`/srv/agent-history` 専用 data disk は 64 GiB 以上
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
├── workspace/projects/       # 管理対象の Git repository
├── data/                     # SQLite、spool、failed
│   └── spool/
│       ├── tmp/
│       ├── pending/
│       └── failed/
├── claude-home/              # 0700、認証情報を含む
├── codex-home/               # 0700、認証情報を含む
├── github-auth/              # 0700、認証情報を含む
├── project-state/            # project.json と生成された progress.md
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

`agent-history-vm-init` は `workspace/projects` と `project-state` も作成します。
agent-shell には projects を `/workspace/projects` として bind mount します。worker
には project worktree、Codex/Claude/GitHub 認証情報を一切 mount しません。

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

## 初回認証と通常運用

認証情報は VM 内の bind mount にだけ保存します。ホスト側の `~/.codex`、`~/.claude`、
`~/.ssh`、GitHub 認証をコピーしたり mount したりしません。VM に SSH で接続した後、
agent-shell 内で個別に認証します。

```bash
cd /srv/agent-history/workspace/agent-history
make vm-codex-login       # 対話的な Codex login
make vm-codex-status
make vm-gh-login          # GitHub device/web login
make vm-gh-status
make vm-claude            # Claude Code の初回対話 login を VM 内で行う
```

通常起動・停止は `sudo systemctl start|stop|restart agent-history-compose.service` を
使います。障害調査は `journalctl -u agent-history-compose.service -b`、
`make vm-status`、`make vm-worker-logs`、`make vm-worker-status` の順で行います。
更新時は host checkout を共有せず、VM 内 checkout で pull、明示 build、systemd restart を
行い、最後に `make vm-test` を実行します。

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

実運用の世代バックアップには、DB 単体 script ではなく次を使います。

```bash
/srv/agent-history/workspace/agent-history/scripts/agent-history-backup
```

この script は SQLite Backup API で DB を online snapshot し、pending/failed spool、
project-state、Compose/systemd/VM 初期化定義、許可リスト化した VM 環境設定を 1 つの
`agent-history-backup-*.tar.gz` にまとめます。アーカイブと内容物の SHA-256、
`PRAGMA integrity_check`、sessions/events/targets/FTS の件数を確認します。認証
ディレクトリは含めません。既定 14 世代は `AGENT_HISTORY_BACKUP_KEEP` で変更できます。

復元は live root を上書きせず、新しい空ディレクトリへだけ行います。

```bash
scripts/agent-history-backup-restore \
  /srv/agent-history/backups/agent-history-backup-YYYYMMDDTHHMMSSZ.tar.gz \
  /srv/agent-history-restore-test
AGENT_HISTORY_DB=/srv/agent-history-restore-test/data/agent_history.db \
  /srv/agent-history/workspace/agent-history/bin/agent-history session-list
```

restore script は archive checksum、全ファイル checksum、SQLite integrity、主要 table
件数、利用可能な場合は FTS query を確認します。検証後に restore test root を本番へ
切り替える場合は、worker を停止して別途承認済みの復旧手順で行います。

## Proxmox での再現可能な作成

このリポジトリの `scripts/proxmox-agent-history-vm-create` は Proxmox **ホスト上でのみ**
実行します。先に `pveversion -v`、`qm list`、`pct list`、`pvesm status`、`free -h`、
`ip -brief link`、cloud image 一覧を採取してから、未使用 VMID・storage・bridge・RAM を
決定してください。VMID または `agent-history-vm` 名の衝突時には、script は何も上書き
しません。

Ubuntu cloud image は取得元の `SHA256SUMS` で検証し、image path と digest を引数で
渡します。script はその digest、選択値、cloud-init snippet を `.provisioning.txt` に
記録します。public SSH key だけを cloud-init に渡し、host `/home`、worktree、private
key、Codex/Claude 認証情報はコピーも mount もしません。

```bash
sudo scripts/proxmox-agent-history-vm-create \
  --vmid SELECTED_ID --storage SELECTED_STORAGE --bridge SELECTED_BRIDGE \
  --image /path/to/noble-server-cloudimg-amd64.img --image-sha256 VERIFIED_SHA256 \
  --ssh-public-key /path/to/amida.pub \
  --snippet-storage SELECTED_SNIPPET_STORAGE \
  --snippet-path /resolved/snippets/agent-history-vm-SELECTED_ID.yaml \
  --memory-mib SELECTED_MEMORY_MIB --ip-config ip=dhcp
```

cloud-init enables QEMU Guest Agent and Docker, mounts/formats only a single
otherwise-unmounted data disk using the `agent-history-data` label, builds the
image explicitly, then enables the Compose systemd unit. It fails rather than
guessing if the guest sees zero or more than one candidate data disk.

第一段階の Proxmox バックアップは同じ物理ホスト上の保存先を暫定利用します。ホスト障害には耐えないため、後で別ディスク、NAS、Proxmox Backup Server などへ移します。

通常バックアップに認証情報を含めない方針を維持する場合は、将来、履歴データ用ディスクと認証ディレクトリ用ディスクを分離し、Proxmox 側で認証ディスクをバックアップ対象外にします。初期構築ではこの分離方法を確定してから完全 VM バックアップを有効化します。

## 受け入れ確認

1. VM を再起動し、systemd 経由で worker が復帰する。
2. Codex または Claude Code を SSH 後に起動する。
3. hook が `data/spool/pending/` にイベントを置く。
4. worker が SQLite へ取り込み、`session-list` と `search` で確認できる。
5. worker 停止中の pending を再起動後に取り込める。
6. `agent-history-db-backup` の生成 DB を別名で指定し、`session-list` と `search` が成功する。
7. `agent-history-backup` を作成し、`agent-history-backup-restore` で別 root へ復元して
   integrity/table count/FTS と pending/failed/project-state を確認する。
8. `systemctl reboot` 前に `systemctl stop agent-history-compose.service` と worker の
   spool drain 状態を確認し、再起動後に `systemctl is-active`、`docker compose ps`、
   `journalctl -u agent-history-compose.service -b` を確認する。
