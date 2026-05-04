##############################################################################
# Makefile — Agent 2 development & deployment shortcuts
##############################################################################

IMAGE_NAME  ?= agent2
IMAGE_TAG   ?= latest
REGISTRY    ?= your-registry.io/your-project
NAMESPACE   ?= food-app

.PHONY: help install test lint docker-build docker-push \
        k8s-apply k8s-delete k8s-logs k8s-status clean

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Local development ─────────────────────────────────────────────────────────

install:        ## Install Python dependencies into a local venv
	python -m venv .venv && \
	  .venv/bin/pip install --upgrade pip && \
	  .venv/bin/pip install -r requirements.txt

test:           ## Run the full test suite with coverage
	.venv/bin/pytest tests/ -v --cov=app --cov-report=term-missing

lint:           ## Run basic static checks (requires ruff in venv)
	.venv/bin/ruff check app/ tests/ || true

run-local:      ## Run the agent locally (needs Redis + kubeconfig)
	DRY_RUN=true \
	REDIS_HOST=localhost \
	LOG_FORMAT=text \
	.venv/bin/python -m app.main

# ── Docker ────────────────────────────────────────────────────────────────────

docker-build:   ## Build the Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

docker-tag:     ## Tag for the registry
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

docker-push:    ## Build and push image to registry
	$(MAKE) docker-build docker-tag
	docker push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# ── Kubernetes ────────────────────────────────────────────────────────────────

k8s-apply:      ## Apply all Kubernetes manifests (namespace first)
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/serviceaccount.yaml
	kubectl apply -f k8s/role.yaml
	kubectl apply -f k8s/rolebinding.yaml
	kubectl apply -f k8s/service.yaml
	kubectl apply -f k8s/configmap.yaml
	kubectl apply -f k8s/deployment.yaml

k8s-delete:     ## Tear down Agent 2 resources (keeps namespace)
	kubectl delete -f k8s/deployment.yaml --ignore-not-found
	kubectl delete -f k8s/configmap.yaml  --ignore-not-found
	kubectl delete -f k8s/service.yaml    --ignore-not-found

k8s-status:     ## Show pod status in the target namespace
	kubectl get pods -n $(NAMESPACE) -l app=agent2

k8s-logs:       ## Tail logs from the running agent2 pod
	kubectl logs -n $(NAMESPACE) -l app=agent2 -f --tail=100

# ── Misc ──────────────────────────────────────────────────────────────────────

clean:          ## Remove venv, caches, compiled files
	rm -rf .venv __pycache__ app/__pycache__ tests/__pycache__ \
	       .pytest_cache .coverage htmlcov dist build *.egg-info
