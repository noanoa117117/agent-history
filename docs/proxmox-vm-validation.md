# Proxmox VM 検証記録

## 2026-08-12: リポジトリ側検証

この記録は VM 作成前の検証であり、Proxmox 実機上での稼働結果を示すものではありません。

実行環境は Ubuntu の `container-other` で、`pveversion`、`qm`、`pct`、`pvesm` は存在しません
でした。従って、Proxmox version、使用中 VMID、storage/bridge、cloud image、既存 VM/LXC、
実 RAM 予約をこの環境から確認・変更していません。configured SSH host もありませんでした。

GitHub の正本は `origin/main` を `git ls-remote` で確認し、
`9f01f875f9a78673439c5f05f464cbf57f741fc2`（2026-08-03、Prepare agent-history for Proxmox VM deployment）
と local `origin/main` が一致しました。

成功したローカル検証:

- `python3 -m compileall -q src tests`
- shell syntax、Codex hooks JSON、cloud-init YAML、systemd unit、Compose bind-mount config
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`（113 tests）
- Claude Code hook と Codex hook を同一 spool に投入し、worker drain 後に各 1 event、FTS 検索、
  `PRAGMA integrity_check` を確認
- SQLite Backup API を用いた archive 作成、checksum、pending/failed spool と project-state を
  含む別 root への restore、table count と FTS 検索を確認

## 実機で残る確認

Proxmox node への管理アクセス後、作成 script 実行前に read-only survey をやり直す必要があります。
その結果で VMID、storage、bridge、RAM、IP と cloud image checksum を決定します。VM 作成後は
QEMU Guest Agent、SSH、container recreate 後の data、systemd boot recovery、実際の Codex/Claude
interactive authentication と hook E2E をこの文書へ追記します。
