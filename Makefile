# =============================================================================
# Makefile — Reverse Document Generator (Flask Web Server)
# =============================================================================
#
# Build, test, run, lint, and deploy commands for the Flask-based
# Reverse Document Generator. This Makefile replaces the original Cloud Run
# Job Makefile (which used `deploy-to-cloud-run --type job`) with Cloud Run
# Service deployment targets via `gcloud run services deploy`.
#
# Usage:
#   make help                  — Show available targets
#   make init                  — Install all dependencies (production + dev)
#   make run                   — Start Flask development server on port 8080
#   make test                  — Run pytest with coverage reporting
#   make lint                  — Run flake8 and black --check
#   make build                 — Build Docker image with BuildKit
#   make deploy                — Build, push, and deploy to Cloud Run Service
#   make clean                 — Remove Docker images and Python cache files
#   make install-deployment-utils — Install Blitzy deployment utilities
#
# =============================================================================

# Ensure all targets use bash for consistent behavior
SHELL := /bin/bash

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------

# Application
APP_NAME            := archie-job-reverse-document-generator
PORT                ?= 8080
FLASK_APP           ?= app.main:application
FLASK_ENV           ?= development

# Docker
IMAGE_NAME          := $(APP_NAME)
DOCKER_TAG          ?= latest

# Google Cloud — Artifact Registry
ARTIFACTORY_REGION  ?= us-east1
PROJECT_ID_DEV      ?= blitzy-os-dev
PROJECT_ID_STAGE    ?= blitzy-platform-stage
REPOSITORY          ?= docker-$(ARTIFACTORY_REGION)
REGISTRY            := $(ARTIFACTORY_REGION)-docker.pkg.dev/$(PROJECT_ID_STAGE)/$(REPOSITORY)
FULL_IMAGE          := $(REGISTRY)/$(IMAGE_NAME):$(DOCKER_TAG)

# Deployment — Cloud Run Service
ENV                 ?= dev
CLOUD_RUN_REGION    ?= us-east1
SERVICE_ACCOUNT     ?= $(APP_NAME)@$(PROJECT_ID_DEV).iam.gserviceaccount.com
MEMORY              ?= 4Gi
CPU                 ?= 2
TIMEOUT             ?= 3600
MAX_INSTANCES       ?= 10
MIN_INSTANCES       ?= 0
CONCURRENCY         ?= 10

# Testing
COV_MIN             ?= 70

# Python
PYTHON              ?= python3
PIP                 ?= pip3

# Google Cloud credentials path (for Docker build secret mount)
GOOGLE_CREDS        ?= $(HOME)/.config/gcloud/application_default_credentials.json

# -----------------------------------------------------------------------------
# Phony Targets
# -----------------------------------------------------------------------------

.PHONY: help init install install-dev run run-gunicorn build push deploy \
        deploy-image-only test test-unit test-integration lint format check \
        clean clean-docker clean-pyc docker-shell shell install-deployment-utils

# Default target
.DEFAULT_GOAL := help

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

help: ## Show this help message
	@echo ""
	@echo "Reverse Document Generator (Flask) — Make Targets"
	@echo "================================================="
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}'
	@echo ""

# -----------------------------------------------------------------------------
# Initialization & Dependency Installation
# -----------------------------------------------------------------------------

init: install install-dev ## Install all dependencies (production + development)
	@echo "All dependencies installed successfully."

install: ## Install production dependencies from requirements.txt
	$(PIP) install -r requirements.txt

install-dev: ## Install development dependencies from requirements-dev.txt
	$(PIP) install -r requirements-dev.txt

install-deployment-utils: ## Install Blitzy Cloud Run deployment utilities
	$(PIP) install deploy-to-cloud-run

# -----------------------------------------------------------------------------
# Local Development
# -----------------------------------------------------------------------------

run: ## Start Flask development server on port 8080
	FLASK_APP=$(FLASK_APP) \
	FLASK_ENV=$(FLASK_ENV) \
	$(PYTHON) -m flask run --host=0.0.0.0 --port=$(PORT) --reload

run-gunicorn: ## Start Gunicorn production server locally
	PORT=$(PORT) gunicorn --config gunicorn.conf.py $(FLASK_APP)

shell: ## Open a Python shell within the Flask application context
	FLASK_APP=$(FLASK_APP) \
	$(PYTHON) -m flask shell

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------

test: ## Run full test suite with coverage reporting
	$(PYTHON) -m pytest tests/ \
		-v \
		--tb=short \
		--cov=app \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		--cov-fail-under=$(COV_MIN) \
		--no-header \
		-x

test-unit: ## Run unit tests only
	$(PYTHON) -m pytest tests/unit/ \
		-v \
		--tb=short \
		--cov=app \
		--cov-report=term-missing \
		--no-header

test-integration: ## Run integration tests only
	$(PYTHON) -m pytest tests/integration/ \
		-v \
		--tb=short \
		--cov=app \
		--cov-report=term-missing \
		--no-header

# -----------------------------------------------------------------------------
# Code Quality
# -----------------------------------------------------------------------------

lint: ## Run flake8 linter and black format checker
	$(PYTHON) -m flake8 app/ tests/
	$(PYTHON) -m black --check --diff app/ tests/

format: ## Auto-format code with black
	$(PYTHON) -m black app/ tests/

check: lint test ## Run all quality checks (lint + test)

# -----------------------------------------------------------------------------
# Docker Build
# -----------------------------------------------------------------------------

build: ## Build Docker image with BuildKit and GCP credentials secret
	DOCKER_BUILDKIT=1 docker build \
		--secret id=google_creds,src=$(GOOGLE_CREDS) \
		-t $(IMAGE_NAME):$(DOCKER_TAG) \
		-t $(FULL_IMAGE) \
		.
	@echo "Docker image built: $(IMAGE_NAME):$(DOCKER_TAG)"

push: ## Push Docker image to Google Artifact Registry
	docker push $(FULL_IMAGE)
	@echo "Image pushed: $(FULL_IMAGE)"

docker-shell: ## Open an interactive shell inside the Docker container
	docker run --rm -it \
		--env-file .env \
		-p $(PORT):$(PORT) \
		$(IMAGE_NAME):$(DOCKER_TAG) /bin/bash

# -----------------------------------------------------------------------------
# Deployment — Cloud Run Service
# -----------------------------------------------------------------------------
# CRITICAL CHANGE: The original Makefile used `deploy-to-cloud-run --type job`
# for Cloud Run Job deployment. This has been replaced with
# `gcloud run services deploy` for Cloud Run Service deployment, reflecting
# the architectural transformation from a batch job to a persistent Flask
# web server.
# -----------------------------------------------------------------------------

deploy: build push ## Build, push, and deploy to Cloud Run Service
	gcloud run services deploy $(APP_NAME) \
		--image=$(FULL_IMAGE) \
		--region=$(CLOUD_RUN_REGION) \
		--project=$(PROJECT_ID_DEV) \
		--platform=managed \
		--port=$(PORT) \
		--memory=$(MEMORY) \
		--cpu=$(CPU) \
		--timeout=$(TIMEOUT) \
		--max-instances=$(MAX_INSTANCES) \
		--min-instances=$(MIN_INSTANCES) \
		--concurrency=$(CONCURRENCY) \
		--service-account=$(SERVICE_ACCOUNT) \
		--no-allow-unauthenticated \
		--quiet
	@echo "Deployed $(APP_NAME) to Cloud Run Service in $(CLOUD_RUN_REGION)"

deploy-image-only: push ## Deploy with existing image (skip Docker build)
	gcloud run services deploy $(APP_NAME) \
		--image=$(FULL_IMAGE) \
		--region=$(CLOUD_RUN_REGION) \
		--project=$(PROJECT_ID_DEV) \
		--platform=managed \
		--quiet
	@echo "Updated $(APP_NAME) with image $(FULL_IMAGE)"

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------

clean: clean-docker clean-pyc ## Remove all build artifacts and cache files
	@echo "Cleanup complete."

clean-docker: ## Remove Docker images for this project
	-docker rmi $(IMAGE_NAME):$(DOCKER_TAG) 2>/dev/null || true
	-docker rmi $(FULL_IMAGE) 2>/dev/null || true
	@echo "Docker images removed."

clean-pyc: ## Remove Python cache files, coverage data, and build artifacts
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	@echo "Python cache files removed."
