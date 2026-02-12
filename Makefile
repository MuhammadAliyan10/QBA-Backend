SHELL := /bin/bash

.PHONY: proto up down clean run-go run-python help

# Colors
GREEN=\033[0;32m
BLUE=\033[0;34m
YELLOW=\033[1;33m
NC=\033[0m

help:
	@echo -e "$(GREEN)Available Commands:$(NC)"
	@echo "  make proto       -> Generate protobuf (Go + Python)"
	@echo "  make up          -> Start full infra (docker-compose)"
	@echo "  make down        -> Stop infra"
	@echo "  make clean       -> Remove infra & generated files"
	@echo "  make run-go      -> Start Go Control Plane"
	@echo "  make run-python  -> Start Python Worker"
	@echo ""
	@echo -e "$(BLUE)Enterprise Commands:$(NC)"
	@echo "  make audit       -> Run security audit (bandit + gosec)"
	@echo "  make test        -> Run all tests with coverage"
	@echo "  make install-deps -> Install all dependencies"
	@echo "  make docker-build -> Build production Docker images"

# ----------------------------------------------------------
# 1. CODE GENERATION
# ----------------------------------------------------------

proto:
	@echo -e "$(BLUE)[Proto] Generating Protobuf Code...$(NC)"

	# Create output folders
	mkdir -p api/gen/go/v1
	mkdir -p api/gen/python/v1

	# ----------------------
	# GO Code Generation
	# ----------------------
	@echo -e "$(YELLOW)[Proto] Generating Go gRPC Code...$(NC)"
	protoc --proto_path=api/proto/v1 \
		--go_out=api/gen/go/v1 --go_opt=paths=source_relative \
		--go-grpc_out=api/gen/go/v1 --go-grpc_opt=paths=source_relative \
		api/proto/v1/*.proto

	# ----------------------
	# Python Code Generation
	# ----------------------
	@echo -e "$(YELLOW)[Proto] Generating Python gRPC Code...$(NC)"
	python3 -m grpc_tools.protoc -Iapi/proto/v1 \
		--python_out=api/gen/python/v1 \
		--grpc_python_out=api/gen/python/v1 \
		api/proto/v1/*.proto

	# ----------------------
	# Fix Python import issues (macOS + Linux compatible)
	# ----------------------
	@echo -e "$(YELLOW)[Proto] Fixing Python imports...$(NC)"
	@if [[ "$$(uname)" == "Darwin" ]]; then \
		sed -i '' 's/import events_pb2/from . import events_pb2/' api/gen/python/v1/events_pb2_grpc.py || true; \
	else \
		sed -i 's/import events_pb2/from . import events_pb2/' api/gen/python/v1/events_pb2_grpc.py || true; \
	fi

	@echo -e "$(GREEN)[Proto] Code Generation Complete$(NC)"

# ----------------------------------------------------------
# 2. INFRASTRUCTURE
# ----------------------------------------------------------

up:
	@echo -e "$(BLUE)[Infra] Starting Local Cloud...$(NC)"
	docker-compose up -d

down:
	@echo -e "$(YELLOW)[Infra] Stopping Local Cloud...$(NC)"
	docker-compose down

clean:
	@echo -e "$(YELLOW)[Infra] Cleaning up system & generated code...$(NC)"
	docker-compose down -v
	rm -rf api/gen

# ----------------------------------------------------------
# 3. RUN SERVICES
# ----------------------------------------------------------

run-go:
	@echo -e "$(GREEN)[Go] Starting Control Plane...$(NC)"
	cd apps/control-plane && go run cmd/server/main.go

run-python:
	@echo -e "$(GREEN)[Python] Starting Execution Worker...$(NC)"
	cd apps/execution-plane && ./venv/bin/python src/worker.py

# ----------------------------------------------------------
# 4. ENTERPRISE - SECURITY & TESTING
# ----------------------------------------------------------

# Security Audit
audit:
	@echo -e "$(BLUE)[Audit] Running Security Scan...$(NC)"
	@echo -e "$(YELLOW)[Audit] Scanning Python code...$(NC)"
	@cd apps/execution-plane && python3 -m bandit -r src/ -f json -o security-report.json || true
	@cd apps/execution-plane && python3 -m bandit -r src/
	@echo ""
	@echo -e "$(YELLOW)[Audit] Scanning Go code...$(NC)"
	@cd apps/control-plane && gosec -fmt=json -out=security-report.json ./... || true
	@cd apps/control-plane && gosec ./...
	@echo -e "$(GREEN)[Audit] Security scan complete. Check security-report.json files.$(NC)"

# Run Tests
test:
	@echo -e "$(BLUE)[Test] Running tests...$(NC)"
	@echo -e "$(YELLOW)[Test] Go tests...$(NC)"
	@cd apps/control-plane && go test -v -cover ./...
	@echo ""
	@echo -e "$(YELLOW)[Test] Python tests...$(NC)"
	@cd apps/execution-plane && pytest tests/ -v --cov=src --cov-report=term-missing
	@echo -e "$(GREEN)[Test] Tests complete.$(NC)"

# Install Dependencies
install-deps:
	@echo -e "$(BLUE)[Setup] Installing Dependencies...$(NC)"
	@echo -e "$(YELLOW)[Setup] Python dependencies...$(NC)"
	@cd apps/execution-plane && pip3 install -r requirements.txt
	@cd apps/execution-plane && pip3 install -r requirements-dev.txt
	@echo -e "$(YELLOW)[Setup] Playwright browsers...$(NC)"
	@playwright install chromium
	@echo -e "$(YELLOW)[Setup] Go dependencies...$(NC)"
	@cd apps/control-plane && go mod download
	@echo -e "$(YELLOW)[Setup] Security tools...$(NC)"
	@pip3 install bandit
	@go install github.com/securego/gosec/v2/cmd/gosec@latest
	@echo -e "$(GREEN)[Setup] All dependencies installed.$(NC)"

# Build Docker Images
docker-build:
	@echo -e "$(BLUE)[Docker] Building Docker images...$(NC)"
	@docker build -t quanta-control-plane:prod apps/control-plane
	@docker build -t quanta-execution-plane:prod apps/execution-plane
	@docker images | grep quanta
	@echo -e "$(GREEN)[Docker] Docker images built.$(NC)"

# Database Migration
migrate:
	@echo -e "$(BLUE)[DB] Running migrations...$(NC)"
	@for f in migrations/*.sql; do \
		echo -e "$(YELLOW)[DB] Applying $$f...$(NC)"; \
		PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d quanta < $$f; \
	done
	@echo -e "$(GREEN)[DB] Migrations complete.$(NC)"
