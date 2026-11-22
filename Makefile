COMPOSE = docker compose -f docker/compose.yaml --env-file .env
API ?= http://localhost:8000
Q ?= How long is the refund window for annual plans?
TEST_DATABASE_URL ?= postgresql://chatbot:chatbot@localhost:5432/chatbot_test

.PHONY: help install up down logs migrate ingest ask ask-stream api-key test test-unit test-db lint format check-config

help:          ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

install:       ## install dependencies with uv
	uv sync

up:            ## start postgres, api and worker in Docker
	$(COMPOSE) up --build -d
	@echo "API on $(API) (docs at $(API)/docs)"

down:          ## stop the stack
	$(COMPOSE) down

logs:          ## follow api and worker logs
	$(COMPOSE) logs -f api worker

migrate:       ## apply database migrations (local)
	uv run chatbot migrate

ingest:        ## sync configured sources and wait for the result
	$(COMPOSE) exec api chatbot ingest --wait

ask:           ## ask a question over HTTP: make ask Q="..." KEY=cb_...
	@curl -s $(API)/v1/chat -H "X-API-Key: $(KEY)" -H "Content-Type: application/json" \
		-d '{"message": "$(Q)"}' | python3 -m json.tool

ask-stream:    ## same, as Server-Sent Events
	@curl -sN $(API)/v1/chat -H "X-API-Key: $(KEY)" -H "Content-Type: application/json" \
		-d '{"message": "$(Q)", "stream": true}'

api-key:       ## create an API key and the hash for CHATBOT_API_KEYS
	@uv run chatbot hash-key

test:          ## all tests (Postgres tests skip unless TEST_DATABASE_URL is set)
	uv run pytest

test-unit:     ## fast tests, no database
	uv run pytest tests/unit

test-db:       ## integration + e2e tests against Postgres (create the chatbot_test database first)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest tests/integration tests/e2e

lint:          ## ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

format:        ## apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

check-config:  ## print the merged configuration
	uv run chatbot check-config
