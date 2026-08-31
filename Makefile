# Optional make aliases. PowerShell 7 is the primary Windows entry point.
.DEFAULT_GOAL := help
.PHONY: help dev check migrate api web worker test test-api test-web lint build typecheck generate-client e2e check-baseline compose-dev compose-up compose-start compose-stop compose-build compose-config

help dev:
	@echo "Windows: pwsh -NoProfile -File scripts/dev.ps1 check"
	@echo "Run scripts/dev.ps1 api and scripts/dev.ps1 web in separate terminals. Ctrl+C stops each."

check migrate api web worker test test-api test-web lint build typecheck generate-client e2e:
	pwsh -NoProfile -File scripts/dev.ps1 $@

check-baseline:
	uv run --project apps/api --no-sync python scripts/check_repository_baseline.py

# Explicit integration only; no source mounts or hot reload. Native targets never use this.
COMPOSE = docker compose --env-file .env.compose --profile integration

compose-config:
	$(COMPOSE) config --quiet

compose-build:
	$(COMPOSE) build

compose-up compose-dev:
	$(COMPOSE) up -d --wait

compose-start:
	$(COMPOSE) start

compose-stop:
	$(COMPOSE) stop
