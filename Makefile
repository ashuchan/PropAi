IMAGE_NAME ?= jugnu
IMAGE_TAG  ?= local

.PHONY: build
build:  ## Build the Docker image locally
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

.PHONY: smoke
smoke: build  ## Run the entrypoint sanity check
	docker run --rm $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: smoke-shard
smoke-shard: build  ## Run shard entry stub with fake task env
	docker run --rm \
		-e CLOUD_RUN_TASK_INDEX=0 \
		-e CLOUD_RUN_TASK_COUNT=1 \
		$(IMAGE_NAME):$(IMAGE_TAG) \
		python ma_poc/scripts/runners/shard_entry.py

.PHONY: smoke-retry
smoke-retry: build  ## Run retry entry stub
	docker run --rm \
		-e RETRY_MODE=errors \
		$(IMAGE_NAME):$(IMAGE_TAG) \
		python ma_poc/scripts/runners/retry_entry.py

.PHONY: shell
shell: build  ## Open a shell in the image for debugging
	docker run --rm -it --entrypoint bash $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: size
size: build  ## Report the image size and layer breakdown
	@docker images $(IMAGE_NAME):$(IMAGE_TAG) --format 'size: {{.Size}}'
	@echo "---"
	@docker history $(IMAGE_NAME):$(IMAGE_TAG) --format 'table {{.CreatedBy}}\t{{.Size}}' | head -20

.PHONY: test
test:  ## Run all unit tests
	cd ma_poc && pytest tests/ -v --tb=short

.PHONY: lint
lint:  ## Run ruff linter
	cd ma_poc && ruff check ma_poc/ scripts/ tests/

.PHONY: fmt
fmt:  ## Format code with ruff
	cd ma_poc && ruff format ma_poc/ scripts/ tests/
