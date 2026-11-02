# Convenience wrapper around docker compose for the online deploy.
# Usage: make up | make down | make logs | make ps | make build | make pull

.PHONY: up down restart build logs ps

up:          ## Build (if needed) and start all three services in the background
	docker compose up -d --build

down:        ## Stop and remove the containers (keeps the polling-offset volume)
	docker compose down

restart:     ## Restart all services
	docker compose restart

build:       ## Rebuild images without starting
	docker compose build

logs:        ## Tail logs from all services (Ctrl+C to stop following)
	docker compose logs -f --tail=100

ps:          ## Show service status
	docker compose ps
