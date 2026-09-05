.PHONY: dev-build dev-start dev-host-status dev-host-pull dev-shell dev-claude dev-codex dev-codex-external-sandbox dev-codex-login \
	dev-codex-status dev-gh-login dev-gh-status \
	dev-git-status dev-git-log dev-pull dev-push dev-test dev-logs dev-status \
	dev-worker-start dev-worker-stop dev-worker-restart dev-worker-status \
	dev-worker-logs dev-worker-drain dev-stop dev-clean dev-purge \
	vm-build vm-start vm-shell vm-claude vm-codex vm-codex-external-sandbox \
	vm-codex-login vm-codex-status vm-gh-login vm-gh-status vm-git-status \
	vm-git-log vm-pull vm-push vm-test vm-logs vm-status vm-worker-start \
	vm-worker-stop vm-worker-restart vm-worker-status vm-worker-logs \
	vm-worker-drain vm-stop vm-clean vm-purge \
	vm-project-claude vm-project-codex

VM_ENV_FILE ?= /etc/agent-history/agent-history-vm.env
VM_RUN = AGENT_HISTORY_VM_MODE=1 AGENT_HISTORY_VM_ENV_FILE=$(VM_ENV_FILE) ./scripts/dev-container

dev-build:
	./scripts/dev-container build

dev-start:
	./scripts/dev-container start

dev-host-status:
	./scripts/dev-container host-status

dev-host-pull:
	./scripts/dev-container host-pull

dev-shell:
	./scripts/dev-container shell

dev-claude:
	./scripts/dev-container claude $(CLAUDE_ARGS)

dev-codex:
	./scripts/dev-container codex $(CODEX_ARGS)

dev-codex-external-sandbox:
	./scripts/dev-container codex-external-sandbox $(CODEX_ARGS)

dev-codex-login:
	./scripts/dev-container codex-login

dev-codex-status:
	./scripts/dev-container codex-status

dev-gh-login:
	./scripts/dev-container gh-login

dev-gh-status:
	./scripts/dev-container gh-status

dev-git-status:
	./scripts/dev-container git-status

dev-git-log:
	./scripts/dev-container git-log

dev-pull:
	./scripts/dev-container pull

dev-push:
	./scripts/dev-container push

dev-test:
	./scripts/dev-container test

dev-worker-start:
	./scripts/dev-container worker-start

dev-worker-stop:
	./scripts/dev-container worker-stop

dev-worker-restart:
	./scripts/dev-container worker-restart

dev-worker-status:
	./scripts/dev-container worker-status

dev-worker-logs:
	./scripts/dev-container worker-logs

dev-worker-drain:
	./scripts/dev-container worker-drain

dev-logs:
	./scripts/dev-container logs

dev-status:
	./scripts/dev-container status

dev-stop:
	./scripts/dev-container stop

dev-clean:
	./scripts/dev-container clean

dev-purge:
	./scripts/dev-container purge

# VM targets always select the bind-mount Compose override and its explicit
# environment file.  On a VM checkout, the regular dev-* targets are refused
# by scripts/dev-container so they cannot silently start named volumes.
vm-build:
	$(VM_RUN) build

vm-start:
	$(VM_RUN) start --no-build

vm-shell:
	$(VM_RUN) shell

vm-claude:
	$(VM_RUN) claude $(CLAUDE_ARGS)

vm-codex:
	$(VM_RUN) codex $(CODEX_ARGS)

vm-project-claude:
	$(VM_RUN) project-claude "$(PROJECT)" $(CLAUDE_ARGS)

vm-project-codex:
	$(VM_RUN) project-codex "$(PROJECT)" $(CODEX_ARGS)

vm-codex-external-sandbox:
	$(VM_RUN) codex-external-sandbox $(CODEX_ARGS)

vm-codex-login:
	$(VM_RUN) codex-login

vm-codex-status:
	$(VM_RUN) codex-status

vm-gh-login:
	$(VM_RUN) gh-login

vm-gh-status:
	$(VM_RUN) gh-status

vm-git-status:
	$(VM_RUN) git-status

vm-git-log:
	$(VM_RUN) git-log

vm-pull:
	$(VM_RUN) pull

vm-push:
	$(VM_RUN) push

vm-test:
	$(VM_RUN) test

vm-logs:
	$(VM_RUN) logs

vm-status:
	$(VM_RUN) status

vm-worker-start:
	$(VM_RUN) worker-start --no-build

vm-worker-stop:
	$(VM_RUN) worker-stop

vm-worker-restart:
	$(VM_RUN) worker-restart

vm-worker-status:
	$(VM_RUN) worker-status

vm-worker-logs:
	$(VM_RUN) worker-logs

vm-worker-drain:
	$(VM_RUN) worker-drain

vm-stop:
	$(VM_RUN) stop

vm-clean:
	$(VM_RUN) clean

vm-purge:
	@echo 'vm-purge is intentionally disabled; do not delete VM data or authentication directories.' >&2
	@false
