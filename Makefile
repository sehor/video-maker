.PHONY: dev stop migrate test lint generate-client e2e

dev:
	docker compose up --build

stop:
	docker compose down

migrate:
	docker compose run --rm api alembic upgrade head
	docker compose run --rm web npm run auth:migrate

test:
	docker compose run --rm api pytest -q
	docker compose run --rm web npm run test

lint:
	docker compose run --rm api ruff check app tests
	docker compose run --rm web npm run lint

generate-client:
	docker compose run --rm api python scripts/export_openapi.py
	docker compose run --rm web npm run generate:client

e2e:
	docker compose run --rm web npm run test:e2e
