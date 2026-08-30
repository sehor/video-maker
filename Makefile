.PHONY: dev stop migrate test lint build typecheck generate-client e2e check-baseline

dev:
	docker compose up --build

stop:
	docker compose down

migrate:
	docker compose run --rm api alembic upgrade head
	docker compose run --rm web pnpm run auth:migrate

test:
	docker compose run --rm api pytest -q
	docker compose run --rm web pnpm run test

lint:
	docker compose run --rm api ruff check app tests
	docker compose run --rm web pnpm run lint

build:
	docker compose run --rm web pnpm run build

typecheck:
	docker compose run --rm web pnpm exec vue-tsc --noEmit

generate-client:
	docker compose run --rm api python scripts/export_openapi.py
	docker compose run --rm web pnpm run generate:client

e2e:
	docker compose run --rm web pnpm run test:e2e

check-baseline:
	python3 scripts/check_repository_baseline.py
