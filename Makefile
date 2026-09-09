.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------- stack ---

.PHONY: up
up: ## Bring the whole system up and wait until it is actually serving
	$(COMPOSE) up -d --build --wait
	@echo
	@echo "  API      http://localhost:8000/docs"
	@echo "  MinIO    http://localhost:9001  (minioadmin / minioadmin)"
	@echo "  Keycloak http://localhost:8080  (admin / admin)"
	@echo

.PHONY: down
down: ## Stop everything, keep the data
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop everything and delete the volumes (destructive)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail application logs
	$(COMPOSE) logs -f api worker

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

# ------------------------------------------------------------ migrations ---

.PHONY: migrate
migrate: ## Apply migrations
	$(COMPOSE) run --rm migrate

.PHONY: migration
migration: ## Autogenerate a migration:  make migration m="add invoice table"
	$(COMPOSE) run --rm migrate alembic revision --autogenerate -m "$(m)"

.PHONY: migrate-roundtrip
migrate-roundtrip: ## Prove every migration reverses: upgrade -> downgrade -> upgrade
	$(COMPOSE) run --rm migrate alembic upgrade head
	$(COMPOSE) run --rm migrate alembic downgrade base
	$(COMPOSE) run --rm migrate alembic upgrade head
	@echo "migrations reverse cleanly"

.PHONY: seed
seed: ## Load reference data (idempotent)
	$(COMPOSE) run --rm seed

# ----------------------------------------------------------------- tests ---

.PHONY: test
test: ## Unit tests (no stack required)
	python -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## Integration tests (requires: make up)
	python -m pytest tests/integration -q -m integration

.PHONY: lint
lint: ## ruff + mypy --strict
	python -m ruff check src tests scripts
	python -m ruff format --check src tests scripts
	python -m mypy

.PHONY: fmt
fmt: ## Apply formatting and fixable lints
	python -m ruff format src tests scripts
	python -m ruff check --fix src tests scripts

.PHONY: verify
verify: lint test ## What CI runs on every commit

# ------------------------------------------------------------------ demo ---

.PHONY: smoke
smoke: ## Prove the running stack is healthy end to end
	@bash scripts/smoke.sh
