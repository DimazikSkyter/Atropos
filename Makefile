POWERSHELL ?= powershell.exe
COMPOSE_ENV := $(if $(wildcard infra/.env),--env-file infra/.env,)
COMPOSE := docker compose $(COMPOSE_ENV) -f infra/docker-compose.yml
COMPOSE_MONITORING := docker compose $(COMPOSE_ENV) -f infra/docker-compose-monitoring.yml
SERVICE_ENV := $(if $(wildcard infra/service.env),--env-file infra/service.env,)
COMPOSE_SERVICE := docker compose $(SERVICE_ENV) -f infra/docker-compose-service.yml
ACCOUNT ?=

.PHONY: help up down ps logs we monitoring_up full_down windows-exporter windows-exporter-start windows-exporter-stop

help:
	@echo "make experiment2-up        Deploy cache locality experiment"
	@echo "make experiment2-run       Run fixed load and change key diversity"
	@echo "make experiment2-down      Stop experiment2, keep shared monitoring"
	@echo "make experiment-up         Deploy anomaly experiment"
	@echo "make experiment-test1      Kafka disk stall with fixed k6 load"
	@echo "make experiment-test2      Consumer-to-DB network latency"
	@echo "make experiment-down       Stop experiment, preserve results"
	@echo "make up                     Start windows_exporter and Docker services"
	@echo "make down                   Stop Docker services and windows_exporter"
	@echo "make ps                     Show Docker service status"
	@echo "make logs                   Follow Docker service logs"
	@echo "make windows-exporter       Start windows_exporter only"
	@echo "make windows-exporter-stop  Stop the project-managed windows_exporter"
	@echo "make ui-password-hash       Generate the UI password hash"
	@echo "make user-create ACCOUNT=name  Create an empty account, prompt for password"
	@echo "make service-up             Build and start PostgreSQL, migrations, runtime server and UI"
	@echo "make server-up              Build and start the runtime backend and its database"
	@echo "make ui                     Build and start the complete UI/backend stack in Docker"
	@echo "make ui-local               Run UI on the host with the configuration DB in Docker"
	@echo "make service-ps             Show UI/backend/database status"
	@echo "make service-down           Stop service infrastructure, preserve configuration"

we: windows-exporter-start

monitoring_up: windows-exporter-start
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File infra/scripts/migrate-victoria-metrics.ps1
	$(COMPOSE_MONITORING) up -d

up: windows-exporter-start service-up
	$(COMPOSE) up -d

full_down: windows-exporter-stop

down:
	$(COMPOSE) down
	$(COMPOSE_SERVICE) down
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File infra/scripts/stop-windows-exporter.ps1

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

windows-exporter: windows-exporter-start

windows-exporter-start:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File infra/scripts/start-windows-exporter.ps1

windows-exporter-stop:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File infra/scripts/stop-windows-exporter.ps1

.PHONY: ui ui-local ui-up ui-sync ui-password-hash ui-test user-create

ui-sync:
	uv --directory ui sync --locked

ui-password-hash:
	uv --directory ui run --locked python -m atropos_ui.auth

user-create:
	$(if $(strip $(ACCOUNT)),,$(error Set ACCOUNT: make user-create ACCOUNT=login))
	$(COMPOSE_SERVICE) exec server python -m atropos_ui.config_db create-user --username "$(ACCOUNT)"

ui: service-up

ui-up: service-up

ui-local: config-up
ui-local: export ATROPOS_UI_DEMO = 0
ui-local:
	uv --directory ui run --locked python -m atropos_ui.serve

ui-test:
	uv --directory ui run --locked --group dev pytest
	uv --directory ui run --locked --group dev ruff check .

.PHONY: service-up service-down service-migrate service-logs service-ps service-build service-bootstrap server-up config-up

service-bootstrap:
	$(PYTHON) infra/service/bootstrap.py

service-build: service-bootstrap
	$(COMPOSE_SERVICE) build server ui

service-up: service-bootstrap
	$(COMPOSE_SERVICE) up -d --build --wait server ui

server-up: service-bootstrap
	$(COMPOSE_SERVICE) up -d --build --wait server

config-up: service-bootstrap
	$(COMPOSE_SERVICE) up -d --wait config-postgres
	$(COMPOSE_SERVICE) run --rm config-migrate

service-migrate: service-bootstrap
	$(COMPOSE_SERVICE) run --rm config-migrate

service-down:
	$(COMPOSE_SERVICE) down

service-logs:
	$(COMPOSE_SERVICE) logs --tail 100 -f server ui config-postgres

service-ps:
	$(COMPOSE_SERVICE) ps -a

# Isolated anomaly experiment (Python runner works on Windows and Linux).
PYTHON ?= python
EXPERIMENT_ARGS ?=
.PHONY: experiment-up experiment-test1 experiment-test2 experiment-down

experiment-up:
	$(PYTHON) infra/experiment/run.py up

experiment-test1:
	$(PYTHON) infra/experiment/run.py test1 $(EXPERIMENT_ARGS)

experiment-test2:
	$(PYTHON) infra/experiment/run.py test2 $(EXPERIMENT_ARGS)

experiment-down:
	$(PYTHON) infra/experiment/run.py down

# Cache locality experiment; reuses the existing monitoring stack.
EXPERIMENT2_ARGS ?=
.PHONY: experiment2-up experiment2-run experiment2-down

experiment2-up:
	$(PYTHON) infra/experiment2/run.py up

experiment2-run:
	$(PYTHON) infra/experiment2/run.py run $(EXPERIMENT2_ARGS)

experiment2-down:
	$(PYTHON) infra/experiment2/run.py down

# HTTP callback retry amplification and container OOM.
.PHONY: experiment3-up experiment3-run experiment3-down
experiment3-up:
	$(PYTHON) infra/experiment3/run.py up
experiment3-run:
	$(PYTHON) infra/experiment3/run.py run
experiment3-down:
	$(PYTHON) infra/experiment3/run.py down

# Slow minor-GC trend and response-time distribution, reusing shared monitoring.
.PHONY: experiment4-up experiment4-run experiment4-down
experiment4-up:
	$(PYTHON) infra/experiment4/run.py up
experiment4-run:
	$(PYTHON) infra/experiment4/run.py run
experiment4-down:
	$(PYTHON) infra/experiment4/run.py down
