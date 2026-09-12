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
	@echo "  Console  http://localhost:3000"
	@echo "  Grafana  http://localhost:3001  (no login; local only)"
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

# --build, because migrations are baked into the image rather than mounted (see
# the migrate service in docker-compose.yml). Without it a revision written five
# minutes ago is not in the image and `upgrade head` reports success having
# stopped at the previous one -- which looks exactly like having nothing to do.
.PHONY: migrate
migrate: ## Apply migrations
	$(COMPOSE) run --build --rm migrate

# Host-side deliberately, unlike every other alembic target here. Autogenerate
# needs two things the runtime image does not have and should not have: the
# working tree's models rather than the ones baked into the image, and ruff for
# the post-write hook. Run in the container it silently produces an empty
# revision, because the image's models match the database it is diffing against.
.PHONY: migration
migration: ## Autogenerate a migration:  make migration m="add invoice table"
	@test -n "$(m)" || { echo 'usage: make migration m="what changed"'; exit 1; }
	THRESHOLD_DATABASE_URL=postgresql+asyncpg://threshold:threshold@localhost:5432/threshold 	THRESHOLD_S3_ACCESS_KEY_ID=minioadmin 	THRESHOLD_S3_SECRET_ACCESS_KEY=minioadmin 	python -m alembic revision --autogenerate -m "$(m)"
	@echo "review it before committing: autogenerate does not write partial indexes,"
	@echo "check constraints it cannot infer, or data migrations."

.PHONY: migrate-roundtrip
migrate-roundtrip: ## Prove every migration reverses: upgrade -> downgrade -> upgrade
	$(COMPOSE) run --build --rm migrate alembic upgrade head
	$(COMPOSE) run --rm migrate alembic downgrade base
	$(COMPOSE) run --rm migrate alembic upgrade head
	@echo "migrations reverse cleanly"
	@echo "note: this leaves an empty database. run 'make seed'."

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
	python -m ruff check src tests scripts seeds migrations
	python -m ruff format --check src tests scripts seeds migrations
	python -m mypy

.PHONY: fmt
fmt: ## Apply formatting and fixable lints
	python -m ruff format src tests scripts seeds migrations
	python -m ruff check --fix src tests scripts seeds migrations

.PHONY: console-verify
console-verify: ## Typecheck, lint and build the console (what its CI job runs)
	cd console && npm ci && npm run typecheck && npm run lint && npm run build

.PHONY: check-alerts
check-alerts: ## Prove every alert rule fires when it should, and stays quiet when it should not
	python scripts/check_alert_rules.py

.PHONY: verify
verify: lint check-alerts test ## What CI runs on every commit

# ----------------------------------------------------------------- queue ---

.PHONY: queue-stats
queue-stats: ## Queue depth by state
	$(COMPOSE) exec -T api python -m scripts.queuectl stats

.PHONY: dlq
dlq: ## List unreplayed dead letters
	$(COMPOSE) exec -T api python -m scripts.queuectl dlq

.PHONY: replay
replay: ## Replay a dead letter:  make replay id=<uuid>   (or id=--all)
	$(COMPOSE) exec -T api python -m scripts.queuectl replay $(id)

.PHONY: reap
reap: ## Return jobs whose worker died
	$(COMPOSE) exec -T api python -m scripts.queuectl reap

# ------------------------------------------------------------------ demo ---

.PHONY: smoke
smoke: ## Prove the running stack is healthy end to end
	@bash scripts/smoke.sh

.PHONY: demo-crash
demo-crash: ## SIGKILL a worker mid-job and watch the work survive
	@bash scripts/demo_crash_recovery.sh

.PHONY: check-console
check-console: ## Drive the console as a clerk and a buyer, and check what it renders
	@python scripts/check_console.py

.PHONY: demo-match
demo-match: ## Three invoices, three decisions, and the reason for each
	@python scripts/demo_three_way_match.py

.PHONY: eval
eval: ## Measure the extractor and derive the gate:  make eval [n=400]
	@PYTHONPATH=. python scripts/evaluate.py -n $(or $(n),400)

.PHONY: demo-alert
demo-alert: ## Stop the worker and watch QueueObserverStopped notice (~7 min)
	@python scripts/demo_alert.py
