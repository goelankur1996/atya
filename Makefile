# Usage: make up | down | logs | ps | test
.PHONY: up down logs ps test psql

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100 app

ps:
	docker compose ps

psql:
	docker compose exec db psql -U atya atya

test:
	TEST_DATABASE_URL=$${TEST_DATABASE_URL:?set TEST_DATABASE_URL} pytest -q
