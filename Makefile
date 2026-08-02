.PHONY: dev-build dev-start dev-host-status dev-host-pull dev-shell dev-claude dev-codex dev-codex-login \
	dev-codex-status dev-gh-login dev-gh-status \
	dev-git-status dev-git-log dev-pull dev-push dev-test dev-logs dev-status \
	dev-stop dev-clean dev-purge

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
