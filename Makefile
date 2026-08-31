# Optional make aliases. PowerShell 7 is the primary Windows entry point.
.DEFAULT_GOAL := help
.PHONY: help dev check migrate api web worker test test-api test-web lint build typecheck generate-client e2e check-baseline compose-dev compose-stop

help dev:
	@echo "Windows: pwsh -NoProfile -File scripts/dev.ps1 check"
	@echo "Run scripts/dev.ps1 api and scripts/dev.ps1 web in separate terminals. Ctrl+C stops each."

check migrate api web worker test test-api test-web lint build typecheck generate-client e2e:
	pwsh -NoProfile -File scripts/dev.ps1 $@

check-baseline:
	uv run --project apps/api --no-sync python scripts/check_repository_baseline.py

# Legacy integration aliases only; Compose/CI restructuring is WINDEV-05.
compose-dev:
	docker compose --env-file .env.compose up --build

compose-stop:
	docker compose --env-file .env.compose stop
